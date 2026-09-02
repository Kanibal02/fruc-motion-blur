from __future__ import annotations

import queue
import threading
import unittest
from pathlib import Path

from fruc_app.models import RenderJob, RenderSettings
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


if __name__ == "__main__":
    unittest.main()
