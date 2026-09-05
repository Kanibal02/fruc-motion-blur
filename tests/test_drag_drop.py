from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtWidgets import QApplication

from fruc_app.app import DropZone, FRUCApp
from fruc_app.models import JobStatus, RenderJob


class QtUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_drop_zone_accepts_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "clip.mp4"
            video.touch()
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(video))])
            zone = DropZone()
            self.assertTrue(zone.acceptDrops())
            self.assertEqual(zone.local_paths(mime), [video])

    def test_render_again_resets_a_finished_job(self) -> None:
        job = RenderJob(Path("clip.mp4"), status=JobStatus.DONE, progress=1.0)
        job.output_path = Path("output.mp4")
        job.error = "old"
        FRUCApp._reset_job(job)
        self.assertEqual(job.status, JobStatus.WAITING)
        self.assertEqual(job.progress, 0.0)
        self.assertIsNone(job.output_path)
        self.assertEqual(job.error, "")


if __name__ == "__main__":
    unittest.main()
