"""Render real Qt UI states with explicitly illustrative queue fixtures, without a GPU."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from fruc_app.animation import HighRefreshTween
from fruc_app.app import FRUCApp
from fruc_app.ffmpeg import Capabilities
from fruc_app.models import JobStatus, ProbeInfo, RenderJob, RenderSettings
from tests.qt_support import load_test_fonts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("ui-captures"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    load_test_fonts()
    app.setStyle("Fusion")
    with (
        patch("fruc_app.app.load_settings", return_value=RenderSettings()),
        patch("fruc_app.app.save_settings"),
        patch("fruc_app.app.setup_logging", return_value=logging.getLogger("capture")),
        patch("fruc_app.app.find_binary", return_value=None),
    ):
        window = FRUCApp(start_background_tasks=False)
        window.event_timer.stop()
        window.show()
        window._handle_event({"event": "capabilities", "generation": 0, "capabilities": Capabilities(
            "UI capture fixture (no GPU validation)", True, True, ("h264", "hevc", "av1"), True, True, ("linear", "hermite"),
        )})

        def capture(name: str) -> None:
            app.processEvents()
            HighRefreshTween.finish_all()
            QTest.qWait(100)
            app.processEvents()
            if not window.grab().save(str(args.output / name)):
                raise RuntimeError(f"Could not save {name}")

        capture("studio-dark-empty.png")
        for name, status, multiplier in (
            ("coastal-drive.mp4", JobStatus.DONE, 4),
            ("night-run.mov", JobStatus.RENDERING, 6),
            ("city-lights.mp4", JobStatus.WAITING, None),
        ):
            job = RenderJob(Path("D:/Footage") / name, probe=ProbeInfo(3840, 2160, Fraction(60000, 1001), 24), status=status, render_multiplier=multiplier)
            job.progress = 1.0 if status == JobStatus.DONE else 0.42 if status == JobStatus.RENDERING else 0.0
            job.stage_progress = job.progress
            if status == JobStatus.DONE:
                job.output_path = Path("D:/Exports/coastal-drive_FRUC4x_blur.mp4")
            window.jobs[job.id] = job
            item = QTreeWidgetItem()
            item.setData(0, Qt.ItemDataRole.UserRole, job.id)
            window.job_items[job.id] = item
            window.tree.addTopLevelItem(item)
            window._update_row(job)
            if status == JobStatus.RENDERING:
                active = job
        window.active_job_ids = list(window.jobs)
        window._refresh_queue_summary()
        window.tree.setCurrentItem(window.job_items[active.id])
        window._handle_event({"event": "progress", "job_id": active.id, "fraction": 0.42, "speed": 1.84, "eta": 8, "stage": "Rendering"})
        window._set_rendering_ui(True)
        capture("studio-dark-rendering.png")
        window._change_appearance("Light")
        capture("studio-light-rendering.png")
        window._change_appearance("Dark")
        window._set_rendering_ui(False)
        window.resize(1060, 700)
        capture("studio-compact.png")
        window.resize(1320, 880)
        window.motion_button.setChecked(False)
        window._toggle_advanced(True)
        window._toggle_log()
        app.processEvents()
        window.settings_scroll.verticalScrollBar().setValue(window.settings_scroll.verticalScrollBar().maximum())
        capture("studio-advanced.png")
        window._force_close = True
        window.close()


if __name__ == "__main__":
    main()
