from __future__ import annotations

import queue
import tempfile
import threading
import unittest
import sys
import subprocess
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from fruc_app.models import JobStatus, ProbeInfo, RenderJob, RenderSettings
from fruc_app.renderer import Renderer


class ParallelRendererTests(unittest.TestCase):
    def test_render_launch_failure_removes_reserved_intermediate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "video.mp4"
            source.touch()
            renderer = Renderer(Path("ffmpeg"), Path("ffprobe"), queue.Queue())
            job = RenderJob(source)
            with (
                patch("fruc_app.renderer.probe_media", return_value=ProbeInfo(320, 180, Fraction(30), 10)),
                patch.object(renderer, "_run_process", side_effect=OSError("Could not start FFmpeg")),
            ):
                renderer._run_one(job, RenderSettings())
            self.assertEqual(job.status, JobStatus.FAILED)
            self.assertEqual(list(root.iterdir()), [source])

    def test_failed_remux_exposes_intermediate_and_cleans_partial_mp4(self) -> None:
        for failure in (1, OSError("Could not start remux")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "video.mp4"
                source.touch()
                events = queue.Queue()
                renderer = Renderer(Path("ffmpeg"), Path("ffprobe"), events)
                job = RenderJob(source)

                def process(command, job, stage, honor_cancel=True):
                    Path(command[-1]).write_bytes(b"video")
                    if stage == "Remuxing":
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                    return 0

                with (
                    patch("fruc_app.renderer.probe_media", return_value=ProbeInfo(320, 180, Fraction(30), 10)),
                    patch.object(renderer, "_run_process", side_effect=process),
                ):
                    renderer._run_one(job, RenderSettings())
                self.assertEqual(job.status, JobStatus.FAILED)
                self.assertIsNotNone(job.output_path)
                self.assertTrue(job.output_path.is_file())
                self.assertEqual(job.output_path.suffix, ".ts")
                self.assertEqual(list(root.glob("*.mp4")), [source])
                failed = [event for event in list(events.queue) if event.get("status") == JobStatus.FAILED]
                self.assertEqual(failed[0]["output_path"], job.output_path)

    def test_process_handles_are_released_after_success_and_reader_failure(self) -> None:
        for fail in (False, True):
            with self.subTest(fail=fail):
                renderer = Renderer(Path("ffmpeg"), Path("ffprobe"), queue.Queue())
                job = RenderJob(Path("in.mp4"), probe=ProbeInfo(320, 180, Fraction(30), 10))
                command = [sys.executable, "-c", "print('out_time_us=2500000\\nspeed=1.0x\\nprogress=end')"]
                processes = []
                popen = subprocess.Popen

                def record(*args, **kwargs):
                    process = popen(*args, **kwargs)
                    processes.append(process)
                    return process

                with patch("fruc_app.renderer.subprocess.Popen", side_effect=record):
                    if fail:
                        with patch.object(renderer, "_read_progress", side_effect=RuntimeError("reader failed")):
                            with self.assertRaisesRegex(RuntimeError, "reader failed"):
                                renderer._run_process(command, job, "Rendering")
                    else:
                        self.assertEqual(renderer._run_process(command, job, "Rendering"), 0)
                self.assertFalse(renderer._processes)
                self.assertIsNotNone(processes[0].poll())
                self.assertTrue(all(pipe.closed for pipe in (processes[0].stdin, processes[0].stdout, processes[0].stderr)))

    def test_parallel_job_limit_is_honored(self) -> None:
        lock = threading.Lock()
        release = threading.Event()
        active = 0
        maximum = 0

        class TrackingRenderer(Renderer):
            def _run_job(self, job: RenderJob, settings: RenderSettings) -> None:
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                    if active == settings.parallel_jobs:
                        release.set()
                release.wait(timeout=1)
                with lock:
                    active -= 1

        renderer = TrackingRenderer(Path("ffmpeg.exe"), Path("ffprobe.exe"), queue.Queue())
        jobs = [RenderJob(Path(f"video-{index}.mp4")) for index in range(5)]
        self.assertTrue(renderer.start(jobs, RenderSettings(parallel_jobs=3)))
        assert renderer._thread is not None
        renderer._thread.join(timeout=2)
        self.assertFalse(renderer.running)
        self.assertEqual(maximum, 3)

    def test_cancelled_render_is_remuxed_and_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "video.mp4"
            source.write_bytes(b"input")
            job = RenderJob(source)
            renderer = Renderer(Path("ffmpeg.exe"), Path("ffprobe.exe"), queue.Queue())
            calls: list[tuple[str, bool]] = []

            def run_process(
                command: list[str], active_job: RenderJob, stage: str,
                honor_cancel: bool = True,
            ) -> int:
                calls.append((stage, honor_cancel))
                Path(command[-1]).write_bytes(b"partial video")
                if stage == "Rendering":
                    with renderer._state_lock:
                        renderer._cancelled_jobs.add(active_job.id)
                return 0

            renderer._run_process = run_process  # type: ignore[method-assign]
            probe = ProbeInfo(320, 180, Fraction(30), 10)
            settings = RenderSettings(output_same_as_source=False, output_directory=str(root))
            with (
                patch("fruc_app.renderer.probe_media", return_value=probe),
                patch(
                    "fruc_app.renderer.build_render_command",
                    side_effect=lambda ffmpeg, input_path, output, media, render_settings: [
                        "ffmpeg", "-vf", "test", str(output)
                    ],
                ),
                patch(
                    "fruc_app.renderer.build_remux_command",
                    side_effect=lambda ffmpeg, intermediate, output: ["ffmpeg", str(output)],
                ),
            ):
                renderer._run_one(job, settings)

            self.assertEqual(job.status, JobStatus.CANCELLED)
            self.assertEqual(calls, [("Rendering", True), ("Remuxing", False)])
            self.assertIsNotNone(job.output_path)
            assert job.output_path is not None
            self.assertTrue(job.output_path.is_file())
            self.assertIn("output saved", job.error)


if __name__ == "__main__":
    unittest.main()
