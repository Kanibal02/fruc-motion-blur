from __future__ import annotations

import queue
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .ffmpeg import (
    CREATE_NO_WINDOW,
    build_remux_command,
    build_render_command,
    command_text,
    output_paths,
    probe_media,
    progress_seconds,
)
from .models import JobStatus, RenderJob, RenderSettings


class Cancelled(RuntimeError):
    pass


class Renderer:
    def __init__(self, ffmpeg: Path, ffprobe: Path, events: queue.Queue[dict[str, object]]) -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.events = events
        self._thread: threading.Thread | None = None
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._active_jobs: set[str] = set()
        self._cancelled_jobs: set[str] = set()
        self._state_lock = threading.Lock()
        self._stop_queue = threading.Event()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, jobs: list[RenderJob], settings: RenderSettings) -> bool:
        if self.running:
            return False
        self._stop_queue.clear()
        with self._state_lock:
            self._active_jobs.clear()
            self._cancelled_jobs.clear()
        self._thread = threading.Thread(target=self._run_queue, args=(jobs, settings), daemon=True)
        self._thread.start()
        return True

    def cancel_current(self) -> None:
        if not self.running:
            return
        with self._state_lock:
            self._cancelled_jobs.update(self._active_jobs)
            processes = list(self._processes.values())
        for process in processes:
            threading.Thread(target=self._stop_process, args=(process,), daemon=True).start()

    def stop_queue(self) -> None:
        self._stop_queue.set()
        self.cancel_current()

    def _emit(self, event: str, **values: object) -> None:
        self.events.put({"event": event, **values})

    def _status(self, job: RenderJob, status: JobStatus, **values: object) -> None:
        job.status = status
        if status == JobStatus.DONE:
            job.progress = 1.0
        self._emit("status", job_id=job.id, status=status, **values)

    def _run_queue(self, jobs: list[RenderJob], settings: RenderSettings) -> None:
        self._emit("queue_started", parallel=settings.parallel_jobs)
        with ThreadPoolExecutor(max_workers=settings.parallel_jobs) as executor:
            futures = [executor.submit(self._run_one, job, settings) for job in jobs]
            for future in futures:
                future.result()
        self._emit("queue_finished", stopped=self._stop_queue.is_set())

    def _run_one(self, job: RenderJob, settings: RenderSettings) -> None:
        if self._stop_queue.is_set():
            return
        with self._state_lock:
            self._active_jobs.add(job.id)
        try:
            if self._stop_queue.is_set():
                return
            self._run_job(job, settings)
        except Cancelled:
            job.error = "Cancelled by user"
            self._status(job, JobStatus.CANCELLED, error=job.error)
        except Exception as exc:  # Worker boundary: report and continue with the queue.
            job.error = str(exc)
            self._status(job, JobStatus.FAILED, error=job.error)
            self._emit("log", level="ERROR", message=f"{job.input_path.name}: {exc}")
        finally:
            with self._state_lock:
                self._active_jobs.discard(job.id)
                self._cancelled_jobs.discard(job.id)

    def _run_job(self, job: RenderJob, settings: RenderSettings) -> None:
        if not job.input_path.is_file():
            raise RuntimeError("Input file no longer exists")

        self._status(job, JobStatus.PROBING)
        job.probe = probe_media(self.ffprobe, job.input_path)
        self._emit("probed", job_id=job.id, probe=job.probe)
        if self._is_cancelled(job.id):
            raise Cancelled()

        with self._state_lock:
            intermediate_path, mp4_path = output_paths(job.input_path, job.probe, settings)
            intermediate_path.parent.mkdir(parents=True, exist_ok=True)
            intermediate_path.touch(exist_ok=False)
        render_command = build_render_command(
            self.ffmpeg, job.input_path, intermediate_path, job.probe, settings
        )
        filter_option = "-filter_complex" if "-filter_complex" in render_command else "-vf"
        self._emit("command", job_id=job.id, command=command_text(render_command), filter=render_command[render_command.index(filter_option) + 1])
        self._status(job, JobStatus.RENDERING)

        try:
            return_code = self._run_process(render_command, job, "Rendering")
        except Cancelled:
            self._delete_if_present(intermediate_path)
            raise
        if return_code:
            self._delete_if_present(intermediate_path)
            raise RuntimeError(f"FFmpeg render failed with exit code {return_code}")
        if not intermediate_path.is_file() or intermediate_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg completed without producing an output file")

        output = intermediate_path
        if mp4_path:
            remux_command = build_remux_command(self.ffmpeg, intermediate_path, mp4_path)
            self._emit("log", level="INFO", message=f"Remux command: {command_text(remux_command)}")
            self._status(job, JobStatus.REMUXING)
            try:
                return_code = self._run_process(remux_command, job, "Remuxing")
            except Cancelled:
                self._delete_if_present(mp4_path)
                raise
            if return_code:
                self._delete_if_present(mp4_path)
                raise RuntimeError(
                    f"MP4 remux failed; valid intermediate kept at {intermediate_path} "
                    f"(exit code {return_code})"
                )
            if not mp4_path.is_file() or mp4_path.stat().st_size == 0:
                raise RuntimeError(
                    f"MP4 remux produced no file; valid intermediate kept at {intermediate_path}"
                )
            output = mp4_path
            if not settings.keep_ts:
                self._delete_if_present(intermediate_path)

        job.output_path = output
        job.error = ""
        self._status(job, JobStatus.DONE, output_path=output)
        self._emit("log", level="INFO", message=f"Completed: {output}")

    def _run_process(self, command: list[str], job: RenderJob, stage: str) -> int:
        self._emit("log", level="INFO", message=command_text(command))
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        with self._state_lock:
            self._processes[job.id] = process
        if self._is_cancelled(job.id):
            self._stop_process(process)
        stderr_thread = threading.Thread(target=self._read_stderr, args=(process,), daemon=True)
        stderr_thread.start()

        values: dict[str, str] = {}
        assert process.stdout is not None
        for raw in process.stdout:
            if self._is_cancelled(job.id):
                self._stop_process(process)
                break
            line = raw.strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value
            if key == "progress":
                seconds = progress_seconds(values)
                duration = job.probe.duration if job.probe else 0
                fraction = min(0.999, seconds / duration) if duration else 0
                speed_text = values.get("speed", "0x")
                try:
                    speed = float(speed_text.rstrip("x"))
                except ValueError:
                    speed = 0.0
                eta = max(0.0, duration - seconds) / speed if speed > 0 else None
                job.progress = max(job.progress, fraction)
                self._emit(
                    "progress", job_id=job.id, stage=stage, fraction=fraction,
                    processed=seconds, speed=speed, eta=eta,
                )
                values.clear()

        return_code = process.wait()
        stderr_thread.join(timeout=1)
        with self._state_lock:
            if self._processes.get(job.id) is process:
                del self._processes[job.id]
        if self._is_cancelled(job.id):
            raise Cancelled()
        return return_code

    def _is_cancelled(self, job_id: str) -> bool:
        with self._state_lock:
            return job_id in self._cancelled_jobs

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for raw in process.stderr:
            line = raw.rstrip()
            if line:
                self._emit("log", level="FFMPEG", message=line)

    def _stop_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if process.stdin:
                process.stdin.write("q\n")
                process.stdin.flush()
            process.wait(timeout=2)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is not None:
            return
        try:
            process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    @staticmethod
    def _delete_if_present(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
