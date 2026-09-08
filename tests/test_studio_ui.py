from __future__ import annotations

import logging
import os
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from fruc_app.animation import HighRefreshTween
from fruc_app.app import FRUCApp
from fruc_app.ffmpeg import Capabilities
from fruc_app.models import JobStatus, ProbeInfo, RenderJob, RenderSettings
from tests.qt_support import load_test_fonts


class StudioUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        load_test_fonts()
        cls.app.setStyle("Fusion")

    def setUp(self) -> None:
        for name, value in (
            ("load_settings", RenderSettings()),
            ("setup_logging", logging.getLogger("fruc_ui_test")),
            ("find_binary", Path("ffmpeg")),
        ):
            patcher = patch(f"fruc_app.app.{name}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.save = patch("fruc_app.app.save_settings").start()
        self.addCleanup(patch.stopall)
        self.window = FRUCApp(start_background_tasks=False)
        self.window.event_timer.stop()
        self.window.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window._force_close = True
        self.window.close()
        HighRefreshTween.finish_all()
        self.window.deleteLater()
        self.app.processEvents()

    @staticmethod
    def caps(mixers=("linear", "hermite")) -> Capabilities:
        return Capabilities("fixture", True, True, ("h264", "hevc"), True, True, mixers)

    def add_job(self, status=JobStatus.WAITING) -> RenderJob:
        job = RenderJob(Path("example.mp4"), probe=ProbeInfo(1920, 1080, Fraction(60), 10), status=status)
        item = QTreeWidgetItem()
        item.setData(0, Qt.ItemDataRole.UserRole, job.id)
        self.window.jobs[job.id] = job
        self.window.job_items[job.id] = item
        self.window.tree.addTopLevelItem(item)
        self.window._update_row(job)
        self.window.tree.setCurrentItem(item)
        self.window._refresh_queue_summary()
        return job

    def test_stale_gpu_results_and_errors_cannot_replace_current_device(self) -> None:
        self.add_job()
        self.window._capability_generation = 2
        self.window._handle_event({"event": "capabilities", "generation": 1, "capabilities": self.caps()})
        self.assertIsNone(self.window.capabilities)
        self.assertFalse(self.window.start_button.isEnabled())
        current = self.caps()
        self.window._handle_event({"event": "capabilities", "generation": 2, "capabilities": current})
        self.window._handle_event({"event": "capability_error", "generation": 1, "error": "old error"})
        self.assertIs(self.window.capabilities, current)
        self.assertTrue(self.window.start_button.isEnabled())

    def test_changing_gpu_clears_previous_capabilities_before_worker_finishes(self) -> None:
        self.window.capabilities = self.caps()
        self.add_job(JobStatus.DONE)
        with patch("fruc_app.app.threading.Thread"):
            self.window._start_capability_check()
        self.assertIsNone(self.window.capabilities)
        self.assertFalse(self.window.start_button.isEnabled())
        # Selection updates must also respect a pending device check.
        self.window._on_select()
        self.assertFalse(self.window.rerender_button.isEnabled())

    def test_missing_mixers_do_not_crash_preset_selection(self) -> None:
        self.window.capabilities = Capabilities("fixture", True, True, (), False, False, ())
        self.window._apply_preset("Insane")
        self.assertEqual(self.window.multiplier_control.value(), "8×")

    def test_preset_click_updates_actual_render_settings(self) -> None:
        QTest.mouseClick(self.window.preset_buttons["Extra smooth"], Qt.MouseButton.LeftButton)
        settings = self.window._collect_settings()
        self.assertEqual((settings.multiplier, settings.frame_mixer, settings.blur_amount), (6, "linear", 1.0))
        self.assertTrue(self.window.preset_buttons["Extra smooth"].isChecked())
        self.window.blur_slider.setValue(150)
        self.assertFalse(any(button.isChecked() for button in self.window.preset_buttons.values()))

    def test_reduced_motion_finishes_animations_and_persists(self) -> None:
        self.window.current_progress.set_fraction(0.75)
        self.window.motion_button.setChecked(False)
        self.assertEqual(self.window.current_progress.value(), 750)
        self.assertFalse(self.window.current_progress.animation.running)
        self.assertFalse(self.window.drop_zone.artwork.timer.isActive())
        self.assertTrue(self.save.call_args.args[0].reduced_motion)
        self.window._toggle_log()
        self.assertFalse(self.window.log_box.isHidden())
        self.window._toggle_log()
        self.assertTrue(self.window.log_box.isHidden())

    def test_completed_job_keeps_its_sample_count_when_settings_change(self) -> None:
        job = self.add_job(JobStatus.DONE)
        job.render_multiplier = 4
        self.window._apply_preset("Insane")
        self.assertEqual(self.window.job_items[job.id].text(1), "4×")

    def test_rendering_blocks_restart_and_stops_decorative_animation(self) -> None:
        job = self.add_job()
        self.window.capabilities = self.caps()
        self.window.renderer = Mock(running=True)
        self.window._set_rendering_ui(True)
        self.window._begin_render([job])
        self.window.renderer.start.assert_not_called()
        self.assertFalse(self.window.drop_zone.artwork.timer.isActive())
        self.assertFalse(self.window.settings_content.isEnabled())

    def test_late_initial_probe_cannot_overwrite_render_status(self) -> None:
        job = self.add_job(JobStatus.RENDERING)
        self.window.active_job_ids = [job.id]
        self.window._rendering = True
        self.window._handle_event({"event": "probe_failed", "job_id": job.id, "error": "old probe"})
        self.assertEqual(job.status, JobStatus.RENDERING)
        self.assertEqual(job.error, "")

    def test_failed_job_with_no_output_is_handled_and_remux_progress_resets(self) -> None:
        job = self.add_job(JobStatus.RENDERING)
        job.progress = 0.99
        self.window._handle_event({"event": "status", "job_id": job.id, "status": JobStatus.REMUXING})
        self.assertEqual(self.window.job_items[job.id].text(2), "Remuxing 0%")
        self.window._handle_event({"event": "status", "job_id": job.id, "status": JobStatus.FAILED, "output_path": None})
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertFalse(self.window.open_button.isEnabled())

    def test_empty_queue_and_terminal_controls_follow_queue_contents(self) -> None:
        self.window.capabilities = self.caps()
        self.window._refresh_queue_summary()
        self.assertEqual(self.window.queue_stack.currentIndex(), 0)
        self.assertFalse(self.window.start_button.isEnabled())
        self.add_job(JobStatus.DONE)
        self.assertTrue(self.window.clear_button.isEnabled())
        self.window._clear_completed()
        self.assertEqual(self.window.queue_stack.currentIndex(), 0)

    def test_minimum_window_keeps_primary_controls_on_screen(self) -> None:
        self.window.resize(1060, 700)
        self.app.processEvents()
        self.assertEqual(self.window.width(), 1060)
        self.assertEqual(self.window.height(), 700)
        for widget in (self.window.start_button, self.window.open_button, self.window.motion_button):
            with self.subTest(widget=widget.text()):
                bottom_right = widget.mapTo(self.window, QPoint(widget.width() - 1, widget.height() - 1))
                self.assertTrue(self.window.rect().contains(bottom_right))
        for button in self.window.preset_buttons.values():
            self.assertGreaterEqual(button.height(), 77)
        self.assertLessEqual(self.window.settings_content.width(), self.window.settings_scroll.viewport().width())


if __name__ == "__main__":
    unittest.main()
