from __future__ import annotations

import queue
import subprocess
import threading
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
        self._process: subprocess.Popen[str] | None = None
        self._process_lock = threading.Lock()
        self._cancel_current = threading.Event()
        self._stop_queue = threading.Event()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, jobs: list[RenderJob], settings: RenderSettings) -> bool:
        if self.running:
            return False
        self._cancel_current.clear()
        self._stop_queue.clear()
        self._thread = threading.Thread(target=self._run_queue, args=(jobs, settings), daemon=True)
        self._thread.start()
        return True

    def cancel_current(self) -> None:
        if not self.running:
            return
        self._cancel_current.set()
        threading.Thread(target=self._stop_process, daemon=True).start()

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
        self._emit("queue_started")
        for job in jobs:
            if self._stop_queue.is_set():
                break
            self._cancel_current.clear()
            try:
                self._run_job(job, settings)
            except Cancelled:
                job.error = "Cancelled by user"
                self._status(job, JobStatus.CANCELLED, error=job.error)
            except Exception as exc:  # Worker boundary: report and continue with the queue.
                job.error = str(exc)
                self._status(job, JobStatus.FAILED, error=job.error)
                self._emit("log", level="ERROR", message=f"{job.input_path.name}: {exc}")
        self._emit("queue_finished", stopped=self._stop_queue.is_set())

    def _run_job(self, job: RenderJob, settings: RenderSettings) -> None:
        if not job.input_path.is_file():
            raise RuntimeError("Input file no longer exists")

        self._status(job, JobStatus.PROBING)
        job.probe = probe_media(self.ffprobe, job.input_path)
        self._emit("probed", job_id=job.id, probe=job.probe)

        intermediate_path, mp4_path = output_paths(job.input_path, job.probe, settings)
        intermediate_path.parent.mkdir(parents=True, exist_ok=True)
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
        with self._process_lock:
            self._process = process
        stderr_thread = threading.Thread(target=self._read_stderr, args=(process,), daemon=True)
        stderr_thread.start()

        values: dict[str, str] = {}
        assert process.stdout is not None
        for raw in process.stdout:
            if self._cancel_current.is_set():
                self._stop_process()
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
        with self._process_lock:
            if self._process is process:
                self._process = None
        if self._cancel_current.is_set():
            raise Cancelled()
        return return_code

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for raw in process.stderr:
            line = raw.rstrip()
            if line:
                self._emit("log", level="FFMPEG", message=line)

    def _stop_process(self) -> None:
        with self._process_lock:
            process = self._process
        if not process or process.poll() is not None:
            return
        try:
            if process.stdin:
                process.stdin.write("q\n")
                process.stdin.flush()
            process.wait(timeout=2)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.terminate()
            process.wait(timeout=2)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        process.kill()
        process.wait(timeout=2)

    @staticmethod
    def _delete_if_present(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
