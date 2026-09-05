from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QWidget

from fruc_app.app import AnimatedComboBox, DropZone, FRUCApp, SmoothScrollArea, frame_interval_ms
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

    def test_wheel_over_picker_scrolls_panel_without_changing_selection(self) -> None:
        scroll = SmoothScrollArea()
        content = QWidget()
        content.setMinimumHeight(1000)
        picker = AnimatedComboBox(content)
        picker.addItems(["First", "Second"])
        picker.setGeometry(0, 0, 120, 40)
        scroll.setWidget(content)
        scroll.resize(200, 200)
        scroll.show()
        self.qt_app.processEvents()
        wheel = QWheelEvent(
            QPointF(5, 5), QPointF(5, 5), QPoint(), QPoint(0, -120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate, False,
        )
        QApplication.sendEvent(picker, wheel)
        self.assertEqual(picker.currentIndex(), 0)
        self.assertEqual(scroll._scroll_tween.end_value, 82)

    def test_animation_timer_adapts_to_display_refresh_rate(self) -> None:
        expected = {60: 16, 75: 13, 120: 8, 144: 6, 165: 6, 240: 4, 360: 2, 540: 1}
        self.assertEqual({rate: frame_interval_ms(rate) for rate in expected}, expected)


if __name__ == "__main__":
    unittest.main()
