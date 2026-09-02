from __future__ import annotations

import queue
import tempfile
import threading
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from fruc_app.models import JobStatus, ProbeInfo, RenderJob, RenderSettings
from fruc_app.renderer import Renderer


class ParallelRendererTests(unittest.TestCase):
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
