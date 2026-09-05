from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QMimeData,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QCloseEvent,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStyle,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .ffmpeg import (
    VIDEO_EXTENSIONS,
    Capabilities,
    build_render_command,
    command_text,
    detect_capabilities,
    filter_chain,
    output_paths,
    probe_media,
)
from .models import JobStatus, RenderJob, RenderSettings, format_time
from .paths import LOG_DIR, ensure_app_dirs, find_binary
from .renderer import Renderer
from .settings import load_settings, save_settings


PRESETS = {
    "Clean": (4, "linear"),
    "Extra smooth": (6, "linear"),
    "Insane": (8, "linear"),
    "Soft": (4, "hermite"),
}
MIXER_LABELS = {
    "linear": "Linear — more motion blur (default)",
    "hermite": "Hermite — less motion blur",
}
MIXERS_BY_LABEL = {label: mixer for mixer, label in MIXER_LABELS.items()}
CODEC_LABELS = {
    "h264": "H.264 — most compatible",
    "hevc": "H.265 / HEVC — better compression",
    "av1": "AV1 — best compression / newer",
}
CODECS_BY_LABEL = {label: codec for codec, label in CODEC_LABELS.items()}
TERMINAL_STATUSES = {JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}


def setup_logging() -> logging.Logger:
    ensure_app_dirs()
    logger = logging.getLogger("fruc_motion_blur")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_DIR / "fruc-motion-blur.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def app_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#4395f7"))
    painter.drawRoundedRect(3, 3, 58, 58, 15, 15)
    painter.setBrush(QColor("#0a1019"))
    painter.drawRoundedRect(13, 13, 38, 38, 9, 9)
    painter.setPen(QColor("#f7fbff"))
    painter.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "F")
    painter.end()
    return QIcon(pixmap)


def line_icon(kind: str, color: str) -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 2.2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if kind == "trash":
        painter.drawLine(8, 9, 24, 9)
        painter.drawLine(13, 6, 19, 6)
        painter.drawRoundedRect(QRectF(10, 11, 12, 14), 2, 2)
        painter.drawLine(14, 14, 14, 22)
        painter.drawLine(18, 14, 18, 22)
    else:
        painter.drawLine(6, 9, 15, 9)
        painter.drawLine(6, 16, 15, 16)
        painter.drawLine(6, 23, 15, 23)
        painter.drawLine(18, 19, 21, 22)
        painter.drawLine(21, 22, 27, 13)
    painter.end()
    return QIcon(pixmap)


def theme_colors(dark: bool) -> dict[str, str]:
    if dark:
        return {
            "bg": "#080c12", "panel": "#0c121b", "card": "#111925",
            "raised": "#162131", "field": "#0b111a", "hover": "#1b293c",
            "border": "#26354a", "text": "#f4f7fb", "muted": "#8998ad",
            "accent": "#4395f7", "accent_hover": "#62a7fa", "selection": "#214f80",
            "track": "#263247", "success": "#45d58a", "warning": "#e1a14d",
            "danger": "#ee6474",
        }
    return {
        "bg": "#edf2f8", "panel": "#f4f7fb", "card": "#ffffff",
        "raised": "#f4f7fb", "field": "#f7f9fc", "hover": "#e7eef8",
        "border": "#d2dce8", "text": "#172033", "muted": "#65758b",
        "accent": "#287bdc", "accent_hover": "#1769c6", "selection": "#d8eaff",
        "track": "#d8e1ec", "success": "#168651", "warning": "#ad681c",
        "danger": "#c63d50",
    }


def theme_stylesheet(c: dict[str, str]) -> str:
    return f"""
        * {{ font-family: "Segoe UI"; font-size: 10pt; color: {c['text']}; }}
        QWidget#root {{ background: {c['bg']}; }}
        QFrame#appHeader {{ background: {c['panel']}; border-bottom: 1px solid {c['border']}; }}
        QFrame#card, QFrame#settingsCard, QFrame#advancedCard {{
            background: {c['card']}; border: 1px solid {c['border']}; border-radius: 13px;
        }}
        QLabel#brandTitle {{ font-size: 20pt; font-weight: 700; }}
        QLabel#brandSubtitle, QLabel#muted {{ color: {c['muted']}; }}
        QLabel#cardTitle {{ font-size: 13pt; font-weight: 700; }}
        QLabel#sectionTitle {{ font-size: 10pt; font-weight: 700; }}
        QLabel#valueBadge {{
            background: {c['raised']}; border: 1px solid {c['border']}; border-radius: 8px;
            color: {c['text']}; font-weight: 600; padding: 3px 8px;
        }}
        QLabel#statusBadge {{
            background: {c['raised']}; border: 1px solid {c['border']}; border-radius: 10px;
            padding: 5px 10px; font-weight: 600;
        }}
        QLabel#statusBadge[state="ready"] {{ color: {c['success']}; }}
        QLabel#statusBadge[state="warning"] {{ color: {c['warning']}; }}
        QLabel#statusBadge[state="error"] {{ color: {c['danger']}; }}
        QScrollArea {{ background: transparent; border: none; }}
        QScrollArea > QWidget > QWidget {{ background: transparent; }}
        QComboBox, QLineEdit {{
            min-height: 34px; background: {c['field']}; border: 1px solid {c['border']};
            border-radius: 8px; padding: 0 11px; selection-background-color: {c['selection']};
        }}
        QComboBox:hover, QLineEdit:hover {{ border-color: {c['accent']}; }}
        QComboBox:focus, QLineEdit:focus {{ border: 1px solid {c['accent']}; }}
        QComboBox:disabled, QLineEdit:disabled {{ color: {c['muted']}; background: {c['panel']}; }}
        QComboBox::drop-down {{ border: none; width: 28px; }}
        QComboBox QAbstractItemView {{
            background: {c['card']}; border: 1px solid {c['border']}; border-radius: 8px;
            selection-background-color: {c['selection']}; outline: 0; padding: 4px;
        }}
        QComboBox QAbstractItemView::item {{ min-height: 28px; padding: 3px 8px; }}
        QPushButton, QToolButton {{
            min-height: 34px; background: {c['raised']}; border: 1px solid {c['border']};
            border-radius: 8px; padding: 0 13px; font-weight: 600;
        }}
        QPushButton:hover, QToolButton:hover {{ background: {c['hover']}; border-color: {c['accent']}; }}
        QPushButton:pressed, QToolButton:pressed {{ background: {c['selection']}; }}
        QPushButton:disabled, QToolButton:disabled {{
            color: {c['muted']}; background: {c['panel']}; border-color: {c['border']};
        }}
        QPushButton#primaryButton {{
            min-height: 40px; background: {c['accent']}; border-color: {c['accent']}; color: white;
        }}
        QPushButton#primaryButton:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
        QPushButton#warningButton {{ color: {c['warning']}; }}
        QPushButton#dangerButton {{ color: {c['danger']}; }}
        QPushButton[compact="true"] {{ min-height: 30px; padding: 0 10px; }}
        QPushButton[segment="true"] {{
            min-height: 30px; background: {c['field']}; border: 1px solid {c['border']};
            border-radius: 7px; padding: 0 7px;
        }}
        QPushButton[segment="true"]:checked {{
            background: {c['accent']}; border-color: {c['accent']}; color: white;
        }}
        QPushButton#dropZone {{
            min-height: 82px; background: {c['panel']}; border: 2px dashed {c['border']};
            border-radius: 13px; color: {c['muted']}; font-size: 11pt;
        }}
        QPushButton#dropZone:hover, QPushButton#dropZone[dragActive="true"] {{
            background: {c['raised']}; border-color: {c['accent']}; color: {c['text']};
        }}
        QTreeWidget {{
            background: {c['field']}; alternate-background-color: {c['panel']};
            border: 1px solid {c['border']}; border-radius: 9px; outline: 0; padding: 3px;
            selection-background-color: {c['selection']};
        }}
        QTreeWidget::item {{ min-height: 36px; border: none; padding: 2px 5px; }}
        QTreeWidget::item:hover {{ background: {c['hover']}; }}
        QHeaderView::section {{
            background: {c['raised']}; color: {c['muted']}; border: none;
            border-bottom: 1px solid {c['border']}; padding: 8px 7px; font-weight: 600;
        }}
        QProgressBar {{
            min-height: 7px; max-height: 7px; background: {c['track']}; border: none;
            border-radius: 3px;
        }}
        QProgressBar::chunk {{ background: {c['accent']}; border-radius: 3px; }}
        QProgressBar#overallProgress::chunk {{ background: #836ef9; }}
        QSlider::groove:horizontal {{ height: 5px; background: {c['track']}; border-radius: 2px; }}
        QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 2px; }}
        QSlider::handle:horizontal {{
            width: 17px; margin: -6px 0; border-radius: 8px; background: {c['accent']};
            border: 2px solid {c['card']};
        }}
        QCheckBox {{ spacing: 9px; min-height: 26px; }}
        QCheckBox::indicator {{
            width: 17px; height: 17px; border: 1px solid {c['border']}; border-radius: 5px;
            background: {c['field']};
        }}
        QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; }}
        QPlainTextEdit {{
            background: {c['field']}; border: 1px solid {c['border']}; border-radius: 9px;
            padding: 8px; selection-background-color: {c['selection']};
        }}
        QMenu {{ background: {c['card']}; border: 1px solid {c['border']}; padding: 5px; }}
        QMenu::item {{ padding: 7px 24px 7px 10px; border-radius: 6px; }}
        QMenu::item:selected {{ background: {c['selection']}; }}
        QMenu::separator {{ height: 1px; background: {c['border']}; margin: 5px 8px; }}
        QToolTip {{
            background: {c['raised']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 5px;
        }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
        QScrollBar::handle:vertical {{
            background: {c['border']}; min-height: 28px; border-radius: 4px;
        }}
        QScrollBar::handle:vertical:hover {{ background: {c['muted']}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    """


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def make_card(parent: QWidget | None = None, shadow: bool = True) -> QFrame:
    frame = QFrame(parent)
    frame.setObjectName("card")
    if shadow:
        effect = QGraphicsDropShadowEffect(frame)
        effect.setBlurRadius(22)
        effect.setOffset(0, 5)
        effect.setColor(QColor(0, 0, 0, 80))
        frame.setGraphicsEffect(effect)
    return frame


class SmoothProgressBar(QProgressBar):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setRange(0, 1000)
        self.setTextVisible(False)
        self.animation = QPropertyAnimation(self, b"value", self)
        self.animation.setDuration(180)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def set_fraction(self, fraction: float, animate: bool = True) -> None:
        target = round(min(1.0, max(0.0, fraction)) * 1000)
        self.animation.stop()
        if not animate:
            self.setValue(target)
            return
        self.animation.setStartValue(self.value())
        self.animation.setEndValue(target)
        self.animation.start()


def animate_popup(popup: QWidget, owner: QWidget) -> None:
    previous = getattr(owner, "_popup_animation", None)
    if previous:
        previous.stop()
        previous.deleteLater()
    final_position = popup.pos()
    popup.setWindowOpacity(0.0)
    popup.move(final_position + QPoint(0, -7))
    animation = QParallelAnimationGroup(owner)
    fade = QPropertyAnimation(popup, b"windowOpacity", animation)
    fade.setDuration(135)
    fade.setStartValue(0.0)
    fade.setEndValue(1.0)
    fade.setEasingCurve(QEasingCurve.Type.OutCubic)
    slide = QPropertyAnimation(popup, b"pos", animation)
    slide.setDuration(165)
    slide.setStartValue(popup.pos())
    slide.setEndValue(final_position)
    slide.setEasingCurve(QEasingCurve.Type.OutCubic)
    owner._popup_animation = animation  # type: ignore[attr-defined]
    animation.start()


class SmoothScrollArea(QScrollArea):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scroll_target = 0
        self._scroll_animation = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._scroll_animation.setDuration(190)
        self._scroll_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def smooth_wheel(self, event) -> None:  # type: ignore[no-untyped-def]
        bar = self.verticalScrollBar()
        pixels = event.pixelDelta().y()
        if pixels:
            self._scroll_animation.stop()
            bar.setValue(bar.value() - pixels)
            self._scroll_target = bar.value()
            event.accept()
            return
        steps = event.angleDelta().y() / 120
        if not steps:
            event.ignore()
            return
        base = (
            self._scroll_target
            if self._scroll_animation.state() == QAbstractAnimation.State.Running
            else bar.value()
        )
        self._scroll_target = max(bar.minimum(), min(bar.maximum(), round(base - steps * 82)))
        self._scroll_animation.stop()
        self._scroll_animation.setStartValue(bar.value())
        self._scroll_animation.setEndValue(self._scroll_target)
        self._scroll_animation.start()
        event.accept()

    def wheelEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.smooth_wheel(event)


def forward_wheel(widget: QWidget, event) -> None:  # type: ignore[no-untyped-def]
    parent = widget.parentWidget()
    while parent:
        if isinstance(parent, SmoothScrollArea):
            parent.smooth_wheel(event)
            return
        parent = parent.parentWidget()
    event.ignore()


class AnimatedComboBox(QComboBox):
    def showPopup(self) -> None:
        super().showPopup()
        animate_popup(self.view().window(), self)

    def hidePopup(self) -> None:
        animation = getattr(self, "_popup_animation", None)
        if animation:
            animation.stop()
        self.view().window().setWindowOpacity(1.0)
        super().hidePopup()

    def wheelEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        forward_wheel(self, event)


class ScrollSafeSlider(QSlider):
    def wheelEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        forward_wheel(self, event)


class AnimatedMenu(QMenu):
    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        animate_popup(self, self)


class SegmentedControl(QWidget):
    value_changed = Signal(str)

    def __init__(self, values: list[str], value: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._buttons: dict[str, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        for label in values:
            button = QPushButton(label, self)
            button.setCheckable(True)
            button.setProperty("segment", True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, text=label: self.value_changed.emit(text))
            self._group.addButton(button)
            self._buttons[label] = button
            layout.addWidget(button, 1)
        self.set_value(value)

    def value(self) -> str:
        checked = self._group.checkedButton()
        return checked.text() if checked else next(iter(self._buttons))

    def set_value(self, value: str) -> None:
        if value in self._buttons:
            self._buttons[value].setChecked(True)


class DropZone(QPushButton):
    files_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("＋  Drop video files here\n     or click to browse", parent)
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("dragActive", False)

    @staticmethod
    def local_paths(mime: QMimeData) -> list[Path]:
        return [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self.isEnabled() and self.local_paths(event.mimeData()):
            event.acceptProposedAction()
            self.setProperty("dragActive", True)
            repolish(self)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self.isEnabled() and self.local_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.setProperty("dragActive", False)
        repolish(self)
        event.accept()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        paths = self.local_paths(event.mimeData())
        self.setProperty("dragActive", False)
        repolish(self)
        if paths and self.isEnabled():
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class FRUCApp(QMainWindow):
    def __init__(self, start_background_tasks: bool = True) -> None:
        super().__init__()
        self.settings = load_settings()
        self.appearance = self.settings.appearance
        self.logger = setup_logging()
        self.events: queue.Queue[dict[str, object]] = queue.Queue()
        self.ffmpeg = find_binary("ffmpeg")
        self.ffprobe = find_binary("ffprobe")
        self.capabilities: Capabilities | None = None
        self.jobs: dict[str, RenderJob] = {}
        self.job_items: dict[str, QTreeWidgetItem] = {}
        self.active_job_ids: list[str] = []
        self.current_job_id: str | None = None
        self.renderer = Renderer(self.ffmpeg, self.ffprobe, self.events) if self.ffmpeg and self.ffprobe else None
        self._closing = False
        self._force_close = False
        self._close_deadline = 0.0
        self._applying_preset = False
        self.advanced_visible = False
        self.log_visible = False

        self.setWindowTitle("FRUC Motion Blur")
        self.setWindowIcon(app_icon())
        self.resize(1280, 820)
        self.setMinimumSize(1060, 700)
        self._build_ui()
        self._apply_theme()
        self._toggle_advanced(self.settings.advanced_open)
        self._sync_output_controls()
        self._append_log("INFO", "Application started")

        self.event_timer = QTimer(self)
        self.event_timer.setInterval(100)
        self.event_timer.timeout.connect(self._poll_events)
        self.event_timer.start()
        if start_background_tasks:
            QTimer.singleShot(150, self._start_capability_check)

        self.setWindowOpacity(0.0)
        self.fade_animation = QPropertyAnimation(self, b"windowOpacity", self)
        self.fade_animation.setDuration(240)
        self.fade_animation.setStartValue(0.0)
        self.fade_animation.setEndValue(1.0)
        self.fade_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        QTimer.singleShot(0, self.fade_animation.start)

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("root")
        self.setCentralWidget(root)
        page = QVBoxLayout(root)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)

        header = QFrame(root)
        header.setObjectName("appHeader")
        header.setFixedHeight(76)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(22, 12, 22, 12)
        header_layout.setSpacing(12)
        icon_label = QLabel(header)
        icon_label.setPixmap(app_icon().pixmap(38, 38))
        header_layout.addWidget(icon_label)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        title = QLabel("FRUC Motion Blur", header)
        title.setObjectName("brandTitle")
        subtitle = QLabel("Vulkan optical flow  •  temporal motion mixing", header)
        subtitle.setObjectName("brandSubtitle")
        brand.addWidget(title)
        brand.addWidget(subtitle)
        header_layout.addLayout(brand)
        header_layout.addStretch(1)
        self.capability_label = QLabel("●  Checking Vulkan", header)
        self.capability_label.setObjectName("statusBadge")
        self.capability_label.setProperty("state", "warning")
        header_layout.addWidget(self.capability_label)

        self.appearance_button = QToolButton(header)
        self.appearance_button.setObjectName("themeButton")
        self.appearance_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.appearance_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.appearance_button.setMinimumWidth(112)
        theme_menu = AnimatedMenu(self.appearance_button)
        self.theme_actions: dict[str, QAction] = {}
        group = QActionGroup(theme_menu)
        group.setExclusive(True)
        for mode, label in (("Dark", "☾  Dark"), ("Light", "☀  Light"), ("System", "◐  System")):
            action = QAction(label, theme_menu)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, value=mode: self._change_appearance(value))
            group.addAction(action)
            theme_menu.addAction(action)
            self.theme_actions[mode] = action
        self.appearance_button.setMenu(theme_menu)
        header_layout.addWidget(self.appearance_button)
        page.addWidget(header)

        body = QHBoxLayout()
        body.setContentsMargins(20, 16, 20, 20)
        body.setSpacing(16)
        page.addLayout(body, 1)
        left = QWidget(root)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        body.addWidget(left, 1)

        self.drop_zone = DropZone(left)
        self.drop_zone.clicked.connect(self._pick_files)
        self.drop_zone.files_dropped.connect(self._add_paths)
        left_layout.addWidget(self.drop_zone)
        self.selection_label = QLabel("No file selected", left)
        self.selection_label.setObjectName("muted")
        self.selection_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        left_layout.addWidget(self.selection_label)

        queue_card = make_card(left)
        queue_layout = QVBoxLayout(queue_card)
        queue_layout.setContentsMargins(12, 11, 12, 12)
        queue_layout.setSpacing(8)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        queue_title = QLabel("Render queue", queue_card)
        queue_title.setObjectName("cardTitle")
        toolbar.addWidget(queue_title)
        toolbar.addStretch(1)
        self.rerender_button = self._button("Render again", self._rerender_selected, QStyle.StandardPixmap.SP_BrowserReload, compact=True)
        self.rerender_button.setToolTip("Render the selected finished file again with the current settings")
        self.rerender_button.setEnabled(False)
        self.add_button = self._button("Add files", self._pick_files, QStyle.StandardPixmap.SP_DialogOpenButton, compact=True)
        self.remove_button = self._button("Remove", self._remove_selected, compact=True)
        self.clear_button = self._button("Clear finished", self._clear_completed, compact=True)
        for button in (self.rerender_button, self.add_button, self.remove_button, self.clear_button):
            toolbar.addWidget(button)
        queue_layout.addLayout(toolbar)

        self.tree = QTreeWidget(queue_card)
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["File", "Resolution / FPS / Duration", "Samples", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(False)
        self.tree.itemSelectionChanged.connect(self._on_select)
        self.tree.itemDoubleClicked.connect(lambda *_: self._open_selected_folder())
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        queue_layout.addWidget(self.tree, 1)
        left_layout.addWidget(queue_card, 1)

        progress_card = make_card(left)
        progress_layout = QVBoxLayout(progress_card)
        progress_layout.setContentsMargins(14, 12, 14, 13)
        progress_layout.setSpacing(7)
        self.stage_label = QLabel("Idle", progress_card)
        self.stage_label.setObjectName("sectionTitle")
        progress_layout.addWidget(self.stage_label)
        self.current_progress = SmoothProgressBar(progress_card)
        progress_layout.addWidget(self.current_progress)
        self.progress_label = QLabel("0%  •  0.00×  •  ETA --:--:--", progress_card)
        self.progress_label.setObjectName("muted")
        progress_layout.addWidget(self.progress_label)
        self.overall_progress = SmoothProgressBar(progress_card)
        self.overall_progress.setObjectName("overallProgress")
        progress_layout.addWidget(self.overall_progress)
        self.overall_label = QLabel("Queue 0%", progress_card)
        self.overall_label.setObjectName("muted")
        progress_layout.addWidget(self.overall_label)
        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.start_button = self._button("Start queue", self._start_queue, QStyle.StandardPixmap.SP_MediaPlay, "primaryButton")
        self.start_button.setEnabled(False)
        self.cancel_button = self._button("Cancel active", self._cancel_current, QStyle.StandardPixmap.SP_DialogCancelButton, "warningButton")
        self.cancel_button.setEnabled(False)
        self.stop_button = self._button("Stop queue", self._stop_queue, QStyle.StandardPixmap.SP_MediaStop, "dangerButton")
        self.stop_button.setEnabled(False)
        self.open_button = self._button("Open output", self._open_selected_folder, QStyle.StandardPixmap.SP_DirOpenIcon)
        self.open_button.setEnabled(False)
        controls.addWidget(self.start_button, 1)
        controls.addWidget(self.cancel_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.open_button)
        progress_layout.addLayout(controls)
        left_layout.addWidget(progress_card)

        self.log_toggle = QToolButton(left)
        self.log_toggle.setText("Show render log  ▾")
        self.log_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_toggle.clicked.connect(self._toggle_log)
        left_layout.addWidget(self.log_toggle)
        self.log_box = QPlainTextEdit(left)
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(4000)
        self.log_box.setFont(QFont("Cascadia Mono", 9))
        self.log_box.setFixedHeight(145)
        self.log_box.hide()
        left_layout.addWidget(self.log_box)
        self._build_settings(root, body)

    def _build_settings(self, parent: QWidget, body: QHBoxLayout) -> None:
        scroll = SmoothScrollArea(parent)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(370)
        scroll.setMaximumWidth(430)
        self.settings_content = QFrame(scroll)
        self.settings_content.setObjectName("settingsCard")
        panel = QVBoxLayout(self.settings_content)
        panel.setContentsMargins(17, 15, 17, 18)
        panel.setSpacing(7)
        scroll.setWidget(self.settings_content)
        body.addWidget(scroll)
        heading = QHBoxLayout()
        settings_title = QLabel("Render settings", self.settings_content)
        settings_title.setObjectName("cardTitle")
        heading.addWidget(settings_title)
        heading.addStretch(1)
        badge = QLabel("GPU pipeline", self.settings_content)
        badge.setObjectName("valueBadge")
        heading.addWidget(badge)
        panel.addLayout(heading)

        self._section(panel, "Preset")
        self.preset_combo = AnimatedComboBox(self.settings_content)
        self.preset_combo.addItems(["Custom", *PRESETS])
        self.preset_combo.setCurrentText("Custom")
        self.preset_combo.textActivated.connect(self._apply_preset)
        panel.addWidget(self.preset_combo)
        self._section(panel, "Temporal sampling")
        self.multiplier_control = SegmentedControl(["2×", "3×", "4×", "6×", "8×", "12×", "16×"], f"{self.settings.multiplier}×", self.settings_content)
        self.multiplier_control.value_changed.connect(self._settings_changed)
        panel.addWidget(self.multiplier_control)

        self._section(panel, "FRUC optical flow")
        flow = QGridLayout()
        flow.setHorizontalSpacing(8)
        performance_label = QLabel("Performance", self.settings_content)
        performance_label.setObjectName("muted")
        grid_label = QLabel("Flow grid", self.settings_content)
        grid_label.setObjectName("muted")
        flow.addWidget(performance_label, 0, 0)
        flow.addWidget(grid_label, 0, 1)
        self.performance_combo = AnimatedComboBox(self.settings_content)
        self.performance_combo.addItems(["Fast", "Medium", "Slow"])
        self.performance_combo.setCurrentText(self.settings.performance.title())
        self.performance_combo.currentTextChanged.connect(self._settings_changed)
        self.grid_combo = AnimatedComboBox(self.settings_content)
        self.grid_combo.addItems(["1", "2", "4"])
        self.grid_combo.setCurrentText(str(self.settings.grid))
        self.grid_combo.currentTextChanged.connect(self._settings_changed)
        flow.addWidget(self.performance_combo, 1, 0)
        flow.addWidget(self.grid_combo, 1, 1)
        panel.addLayout(flow)
        self._hint(panel, "Fast = quickest; Slow = best matching\nGrid 1 = finest detail; 4 = faster/coarser")

        self._section(panel, "Motion mixer")
        self.mixer_combo = AnimatedComboBox(self.settings_content)
        self.mixer_combo.addItem(MIXER_LABELS.get(self.settings.frame_mixer, MIXER_LABELS["linear"]))
        self.mixer_combo.currentTextChanged.connect(self._settings_changed)
        panel.addWidget(self.mixer_combo)
        self._hint(panel, "Detected libplacebo temporal mixers only")
        self._section(panel, "Blur amount")
        blur_row = QHBoxLayout()
        self.blur_slider = ScrollSafeSlider(Qt.Orientation.Horizontal, self.settings_content)
        self.blur_slider.setRange(25, 200)
        self.blur_slider.setSingleStep(5)
        self.blur_slider.setValue(round(self.settings.blur_amount * 100))
        self.blur_slider.valueChanged.connect(self._blur_changed)
        self.blur_label = QLabel(f"{self.blur_slider.value()}%", self.settings_content)
        self.blur_label.setObjectName("valueBadge")
        blur_row.addWidget(self.blur_slider, 1)
        blur_row.addWidget(self.blur_label)
        panel.addLayout(blur_row)
        self._hint(panel, "100% = original look  •  lower = shorter  •  higher = longer")

        self._section(panel, "Video codec / quality")
        self.codec_combo = AnimatedComboBox(self.settings_content)
        self.codec_combo.addItem(CODEC_LABELS.get(self.settings.video_codec, CODEC_LABELS["h264"]))
        self.codec_combo.currentTextChanged.connect(self._settings_changed)
        panel.addWidget(self.codec_combo)
        self._hint(panel, "Same QP control  •  H.264 is safest for video editors")
        qp_row = QHBoxLayout()
        self.qp_slider = ScrollSafeSlider(Qt.Orientation.Horizontal, self.settings_content)
        self.qp_slider.setRange(18, 40)
        self.qp_slider.setValue(self.settings.qp)
        self.qp_slider.valueChanged.connect(self._qp_changed)
        self.qp_label = QLabel(f"QP {self.settings.qp}", self.settings_content)
        self.qp_label.setObjectName("valueBadge")
        qp_row.addWidget(self.qp_slider, 1)
        qp_row.addWidget(self.qp_label)
        panel.addLayout(qp_row)
        self._section(panel, "Parallel renders")
        self.parallel_control = SegmentedControl(["1", "2", "3", "4"], str(self.settings.parallel_jobs), self.settings_content)
        self.parallel_control.value_changed.connect(self._settings_changed)
        panel.addWidget(self.parallel_control)
        self._hint(panel, "1 = safest  •  higher values share GPU, VRAM, and disk")

        self._section(panel, "Output")
        self.same_output_check = QCheckBox("Save beside source", self.settings_content)
        self.same_output_check.setChecked(self.settings.output_same_as_source)
        self.same_output_check.toggled.connect(self._output_mode_changed)
        panel.addWidget(self.same_output_check)
        output_row = QHBoxLayout()
        self.output_entry = QLineEdit(self.settings.output_directory, self.settings_content)
        self.output_entry.setPlaceholderText("Custom output folder")
        self.output_entry.textChanged.connect(self._settings_changed)
        self.output_browse = self._button("", self._pick_output_directory, QStyle.StandardPixmap.SP_DirOpenIcon, compact=True)
        self.output_browse.setFixedWidth(40)
        output_row.addWidget(self.output_entry, 1)
        output_row.addWidget(self.output_browse)
        panel.addLayout(output_row)
        self.auto_mp4_check = QCheckBox("Automatic MP4 remux", self.settings_content)
        self.auto_mp4_check.setChecked(self.settings.auto_mp4)
        self.auto_mp4_check.toggled.connect(self._settings_changed)
        panel.addWidget(self.auto_mp4_check)
        self.keep_ts_check = QCheckBox("Keep intermediate (TS/MKV)", self.settings_content)
        self.keep_ts_check.setChecked(self.settings.keep_ts)
        self.keep_ts_check.toggled.connect(self._settings_changed)
        panel.addWidget(self.keep_ts_check)

        self.advanced_button = QToolButton(self.settings_content)
        self.advanced_button.setText("Advanced  ▾")
        self.advanced_button.setCheckable(True)
        self.advanced_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.advanced_button.toggled.connect(self._toggle_advanced)
        panel.addWidget(self.advanced_button)
        self.advanced_frame = QFrame(self.settings_content)
        self.advanced_frame.setObjectName("advancedCard")
        advanced = QVBoxLayout(self.advanced_frame)
        advanced.setContentsMargins(11, 11, 11, 11)
        advanced.setSpacing(7)
        advanced.addWidget(QLabel("Vulkan device index", self.advanced_frame))
        self.device_combo = AnimatedComboBox(self.advanced_frame)
        self.device_combo.addItems([str(index) for index in range(8)])
        self.device_combo.setCurrentText(str(self.settings.device_index))
        self.device_combo.textActivated.connect(self._device_changed)
        advanced.addWidget(self.device_combo)
        self.diagnostics_box = QPlainTextEdit(self.advanced_frame)
        self.diagnostics_box.setReadOnly(True)
        self.diagnostics_box.setMaximumHeight(190)
        self.diagnostics_box.setPlainText("Capabilities are being checked…")
        advanced.addWidget(self.diagnostics_box)
        advanced_actions = QHBoxLayout()
        advanced_actions.addWidget(self._button("Copy command", self._copy_command, compact=True), 1)
        advanced_actions.addWidget(self._button("FFmpeg help", lambda: webbrowser.open("https://ffmpeg.org/ffmpeg-filters.html"), compact=True), 1)
        advanced.addLayout(advanced_actions)
        panel.addWidget(self.advanced_frame)
        panel.addStretch(1)

    def _button(self, text: str, slot, standard_icon: QStyle.StandardPixmap | None = None, object_name: str = "", compact: bool = False) -> QPushButton:
        button = QPushButton(text, self)
        if object_name:
            button.setObjectName(object_name)
        if standard_icon is not None:
            button.setIcon(self.style().standardIcon(standard_icon))
            button.setIconSize(QSize(16, 16))
        if compact:
            button.setProperty("compact", True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    @staticmethod
    def _section(layout: QVBoxLayout, text: str) -> None:
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        layout.addSpacing(8)
        layout.addWidget(label)

    @staticmethod
    def _hint(layout: QVBoxLayout, text: str) -> None:
        label = QLabel(text)
        label.setObjectName("muted")
        label.setWordWrap(True)
        layout.addWidget(label)

    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if not app:
            return
        if self.appearance == "System":
            try:
                dark = app.styleHints().colorScheme() == Qt.ColorScheme.Dark
            except AttributeError:
                dark = app.palette().color(QPalette.ColorRole.Window).lightness() < 128
        else:
            dark = self.appearance == "Dark"
        self.colors = theme_colors(dark)
        app.setStyleSheet(theme_stylesheet(self.colors))
        labels = {"Dark": "☾  Dark", "Light": "☀  Light", "System": "◐  System"}
        self.appearance_button.setText(labels[self.appearance])
        self.theme_actions[self.appearance].setChecked(True)
        self.remove_button.setIcon(line_icon("trash", self.colors["danger"]))
        self.clear_button.setIcon(line_icon("clear", self.colors["success"]))
        self.remove_button.setIconSize(QSize(17, 17))
        self.clear_button.setIconSize(QSize(17, 17))
        repolish(self.capability_label)
        for job in self.jobs.values():
            self._update_row(job)

    def _change_appearance(self, value: str) -> None:
        self.appearance = value
        self._apply_theme()
        save_settings(self._collect_settings())

    def _set_capability(self, text: str, state: str) -> None:
        self.capability_label.setText(f"●  {text}")
        self.capability_label.setProperty("state", state)
        repolish(self.capability_label)

    def _start_capability_check(self) -> None:
        if not self.ffmpeg or not self.ffprobe:
            missing = "ffmpeg.exe" if not self.ffmpeg else "ffprobe.exe"
            self._set_capability(f"Missing {missing}", "error")
            self.start_button.setEnabled(False)
            self._append_log("ERROR", f"{missing} not found in ffmpeg/bin or PATH")
            return
        self._set_capability("Checking Vulkan", "warning")
        self.start_button.setEnabled(False)
        self._append_log("INFO", f"FFmpeg: {self.ffmpeg}")
        self._append_log("INFO", f"FFprobe: {self.ffprobe}")
        device = int(self.device_combo.currentText())

        def check() -> None:
            try:
                capabilities = detect_capabilities(self.ffmpeg, device)
                self.events.put({"event": "capabilities", "capabilities": capabilities})
            except Exception as exc:
                self.events.put({"event": "capability_error", "error": str(exc)})

        threading.Thread(target=check, daemon=True).start()

    def _pick_files(self) -> None:
        pattern = " ".join(f"*{extension}" for extension in sorted(VIDEO_EXTENSIONS))
        selected, _ = QFileDialog.getOpenFileNames(self, "Add video files", "", f"Video files ({pattern});;All files (*)")
        if selected:
            self._add_paths([Path(path) for path in selected])

    def _add_paths(self, paths: list[Path]) -> None:
        if self.renderer and self.renderer.running:
            return
        expanded: list[Path] = []
        for path in paths:
            if path.is_dir():
                try:
                    expanded.extend(item for item in path.iterdir() if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS)
                except OSError as exc:
                    self._append_log("ERROR", f"Could not scan {path}: {exc}")
            elif path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                expanded.append(path)
        existing = {os.path.normcase(str(job.input_path.resolve())) for job in self.jobs.values()}
        added: list[RenderJob] = []
        for path in expanded:
            resolved = path.resolve()
            key = os.path.normcase(str(resolved))
            if key in existing:
                continue
            existing.add(key)
            job = RenderJob(resolved, status=JobStatus.PROBING)
            self.jobs[job.id] = job
            item = QTreeWidgetItem([resolved.name, "Inspecting…", self.multiplier_control.value(), JobStatus.PROBING.value])
            item.setData(0, Qt.ItemDataRole.UserRole, job.id)
            item.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))
            item.setToolTip(0, str(resolved))
            self.job_items[job.id] = item
            self.tree.addTopLevelItem(item)
            self._update_row(job)
            added.append(job)
        if added:
            self.tree.setCurrentItem(self.job_items[added[0].id])
            threading.Thread(target=self._probe_jobs, args=(added,), daemon=True).start()
        elif paths:
            self._append_log("WARNING", "No new supported top-level video files were found")

    def _probe_jobs(self, jobs: list[RenderJob]) -> None:
        if not self.ffprobe:
            return
        for job in jobs:
            try:
                info = probe_media(self.ffprobe, job.input_path)
                self.events.put({"event": "probe_ready", "job_id": job.id, "probe": info})
            except Exception as exc:
                self.events.put({"event": "probe_failed", "job_id": job.id, "error": str(exc)})

    def _selected_job(self) -> RenderJob | None:
        item = self.tree.currentItem()
        return self.jobs.get(str(item.data(0, Qt.ItemDataRole.UserRole))) if item else None

    def _remove_selected(self) -> None:
        if self.renderer and self.renderer.running:
            return
        item = self.tree.currentItem()
        job = self._selected_job()
        if not item or not job:
            return
        self.jobs.pop(job.id, None)
        self.job_items.pop(job.id, None)
        self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        self._on_select()

    def _clear_completed(self) -> None:
        if self.renderer and self.renderer.running:
            return
        for job_id, job in list(self.jobs.items()):
            if job.status not in TERMINAL_STATUSES:
                continue
            item = self.job_items.pop(job_id)
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
            del self.jobs[job_id]
        self._on_select()

    def _on_select(self) -> None:
        job = self._selected_job()
        running = bool(self.renderer and self.renderer.running)
        if not job:
            self.selection_label.setText("No file selected")
            self.selection_label.setToolTip("")
            self.open_button.setEnabled(False)
            self.rerender_button.setEnabled(False)
            self._update_diagnostics()
            return
        text = f"{job.input_path}  •  {job.details}"
        self.selection_label.setText(text)
        self.selection_label.setToolTip(text)
        self.open_button.setEnabled(bool(job.output_path))
        self.rerender_button.setEnabled(not running and job.status in TERMINAL_STATUSES)
        self._update_diagnostics()

    def _show_context_menu(self, position) -> None:  # type: ignore[no-untyped-def]
        item = self.tree.itemAt(position)
        if not item:
            return
        self.tree.setCurrentItem(item)
        job = self._selected_job()
        if not job:
            return
        menu = AnimatedMenu(self)
        if job.status in TERMINAL_STATUSES and not (self.renderer and self.renderer.running):
            menu.addAction("↻  Render again", self._rerender_selected)
            menu.addSeparator()
        menu.addAction("Open input folder", lambda: self._open_path(job.input_path.parent))
        if job.output_path:
            menu.addAction("Open output folder", self._open_selected_folder)
            menu.addAction("Copy output path", lambda: QApplication.clipboard().setText(str(job.output_path)))
        menu.addSeparator()
        remove = menu.addAction("Remove", self._remove_selected)
        remove.setEnabled(not (self.renderer and self.renderer.running))
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _start_queue(self) -> None:
        candidates = [job for job in self.jobs.values() if job.status in {JobStatus.WAITING, JobStatus.FAILED, JobStatus.CANCELLED}]
        if not candidates:
            QMessageBox.information(self, "Queue", "Add at least one video or use Render again on a finished item.")
            return
        self._begin_render(candidates)

    def _rerender_selected(self) -> None:
        job = self._selected_job()
        if job and job.status in TERMINAL_STATUSES:
            self._begin_render([job])

    def _begin_render(self, candidates: list[RenderJob]) -> None:
        if not self.renderer or not self.capabilities or not self.capabilities.ready:
            QMessageBox.critical(self, "Cannot render", "Required FFmpeg/Vulkan capabilities are not ready.")
            return
        settings = self._collect_settings()
        if settings.video_codec not in self.capabilities.codecs:
            QMessageBox.critical(self, "Video codec", "The selected codec is not supported by this Vulkan device.")
            return
        if not settings.output_same_as_source:
            if not settings.output_directory:
                QMessageBox.critical(self, "Output folder", "Choose a custom output folder or save beside the source.")
                return
            try:
                Path(settings.output_directory).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.critical(self, "Output folder", str(exc))
                return
        for job in candidates:
            self._reset_job(job)
            self._update_row(job)
        self.settings = settings
        save_settings(settings)
        self.active_job_ids = [job.id for job in candidates]
        if self.renderer.start(candidates, settings):
            self._set_rendering_ui(True)

    @staticmethod
    def _reset_job(job: RenderJob) -> None:
        job.progress = 0.0
        job.error = ""
        job.output_path = None
        job.status = JobStatus.WAITING

    def _cancel_current(self) -> None:
        if self.renderer:
            self.renderer.cancel_current()
            self.stage_label.setText("Cancelling active jobs…")

    def _stop_queue(self) -> None:
        if self.renderer:
            self.renderer.stop_queue()
            self.stage_label.setText("Stopping queue…")

    def _set_rendering_ui(self, running: bool) -> None:
        self.start_button.setEnabled(not running and bool(self.capabilities and self.capabilities.ready))
        self.cancel_button.setEnabled(running)
        self.stop_button.setEnabled(running)
        self.add_button.setEnabled(not running)
        self.remove_button.setEnabled(not running)
        self.clear_button.setEnabled(not running)
        self.drop_zone.setEnabled(not running)
        self.settings_content.setEnabled(not running)
        if not running:
            self._sync_output_controls()
        self._on_select()

    def _poll_events(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass

    def _handle_event(self, event: dict[str, object]) -> None:
        kind = event["event"]
        if kind == "capabilities":
            caps = event["capabilities"]
            assert isinstance(caps, Capabilities)
            self.capabilities = caps
            if caps.ready:
                self._set_capability("Vulkan ready", "ready")
                current_mixer = MIXERS_BY_LABEL.get(self.mixer_combo.currentText(), self.settings.frame_mixer)
                current_codec = CODECS_BY_LABEL.get(self.codec_combo.currentText(), self.settings.video_codec)
                self.mixer_combo.blockSignals(True)
                self.mixer_combo.clear()
                self.mixer_combo.addItems([MIXER_LABELS[mixer] for mixer in caps.mixers])
                self.mixer_combo.setCurrentText(MIXER_LABELS.get(current_mixer, MIXER_LABELS[caps.mixers[0]]))
                self.mixer_combo.blockSignals(False)
                self.codec_combo.blockSignals(True)
                self.codec_combo.clear()
                self.codec_combo.addItems([CODEC_LABELS[codec] for codec in caps.codecs])
                self.codec_combo.setCurrentText(CODEC_LABELS.get(current_codec, CODEC_LABELS[caps.codecs[0]]))
                self.codec_combo.blockSignals(False)
                self.start_button.setEnabled(not (self.renderer and self.renderer.running))
                self._append_log("INFO", f"{caps.version}; mixers: {', '.join(caps.mixers)}; codecs: {', '.join(caps.codecs)}")
            else:
                self._set_capability("Missing: " + ", ".join(caps.missing or ["frame mixer"]), "error")
                self.start_button.setEnabled(False)
                self._append_log("ERROR", self.capability_label.text())
            self._update_diagnostics()
        elif kind == "capability_error":
            self._set_capability("Capability check failed", "error")
            self.start_button.setEnabled(False)
            self._append_log("ERROR", str(event["error"]))
        elif kind in {"probe_ready", "probed"}:
            job = self.jobs.get(str(event["job_id"]))
            if job:
                job.probe = event["probe"]  # type: ignore[assignment]
                if kind == "probe_ready" and job.status == JobStatus.PROBING:
                    job.status = JobStatus.WAITING
                self._update_row(job)
                self._on_select()
        elif kind == "probe_failed":
            job = self.jobs.get(str(event["job_id"]))
            if job:
                job.status = JobStatus.FAILED
                job.error = str(event["error"])
                self._update_row(job)
                self._append_log("ERROR", f"{job.input_path.name}: {job.error}")
        elif kind == "queue_started":
            self.stage_label.setText(f"Queue started  •  {event.get('parallel', 1)} parallel")
        elif kind == "status":
            job = self.jobs.get(str(event["job_id"]))
            if job:
                job.status = event["status"]  # type: ignore[assignment]
                if "error" in event:
                    job.error = str(event["error"])
                if "output_path" in event:
                    job.output_path = Path(event["output_path"])  # type: ignore[arg-type]
                if job.status in {JobStatus.RENDERING, JobStatus.REMUXING, JobStatus.PROBING}:
                    self.current_job_id = job.id
                    self.stage_label.setText(f"{job.status.value}: {job.input_path.name}")
                self._update_row(job)
                self._update_overall()
                self._on_select()
        elif kind == "progress":
            job = self.jobs.get(str(event["job_id"]))
            if job:
                fraction = float(event["fraction"])
                job.progress = max(job.progress, fraction)
                self.current_progress.set_fraction(fraction)
                eta = format_time(event.get("eta") if isinstance(event.get("eta"), (int, float)) else None)
                eta_text = eta if event.get("eta") is not None else "--:--:--"
                self.progress_label.setText(f"{fraction * 100:.1f}%  •  {float(event['speed']):.2f}×  •  ETA {eta_text}")
                self.stage_label.setText(f"{event['stage']}: {job.input_path.name}")
                self._update_row(job)
                self._update_overall()
        elif kind == "command":
            self._append_log("INFO", f"Filter: {event['filter']}")
            self._append_log("INFO", str(event["command"]))
            self._update_diagnostics(str(event["command"]), str(event["filter"]))
        elif kind == "log":
            self._append_log(str(event.get("level", "INFO")), str(event["message"]))
        elif kind == "queue_finished":
            self._set_rendering_ui(False)
            self.current_job_id = None
            self.stage_label.setText("Queue stopped" if event.get("stopped") else "Queue finished")
            self.current_progress.set_fraction(0, animate=False)
            self.progress_label.setText("0%  •  0.00×  •  ETA --:--:--")
            self._update_overall()

    def _update_row(self, job: RenderJob) -> None:
        item = self.job_items.get(job.id)
        if not item:
            return
        media = job.details if job.probe else (job.error or "Inspecting…")
        status = job.status.value
        if job.status in {JobStatus.RENDERING, JobStatus.REMUXING}:
            status = f"{status} {job.progress * 100:.0f}%"
        item.setText(0, job.input_path.name)
        item.setText(1, media)
        item.setText(2, self.multiplier_control.value())
        item.setText(3, status)
        item.setToolTip(3, job.error)
        color = {
            JobStatus.DONE: self.colors["success"] if hasattr(self, "colors") else "#45d58a",
            JobStatus.FAILED: self.colors["danger"] if hasattr(self, "colors") else "#ee6474",
            JobStatus.CANCELLED: self.colors["warning"] if hasattr(self, "colors") else "#e1a14d",
            JobStatus.PROBING: self.colors["accent"] if hasattr(self, "colors") else "#4395f7",
            JobStatus.RENDERING: self.colors["accent"] if hasattr(self, "colors") else "#4395f7",
            JobStatus.REMUXING: self.colors["accent"] if hasattr(self, "colors") else "#4395f7",
        }.get(job.status)
        brush = QBrush(QColor(color)) if color else QBrush()
        for column in range(4):
            item.setForeground(column, brush)

    def _update_overall(self) -> None:
        jobs = [self.jobs[job_id] for job_id in self.active_job_ids if job_id in self.jobs]
        if not jobs:
            self.overall_progress.set_fraction(0, animate=False)
            self.overall_label.setText("Queue 0%")
            return
        weights = [job.probe.duration if job.probe else 1.0 for job in jobs]
        done = sum(weight * (1.0 if job.status in TERMINAL_STATUSES else job.progress) for job, weight in zip(jobs, weights))
        fraction = done / sum(weights)
        self.overall_progress.set_fraction(fraction)
        self.overall_label.setText(f"Queue {fraction * 100:.1f}%")

    def _settings_changed(self, *_args) -> None:
        if self._applying_preset:
            return
        self.preset_combo.setCurrentText("Custom")
        for job in self.jobs.values():
            self._update_row(job)
        self._update_diagnostics()

    def _apply_preset(self, name: str) -> None:
        if name not in PRESETS:
            return
        multiplier, mixer = PRESETS[name]
        if self.capabilities and mixer not in self.capabilities.mixers:
            mixer = self.capabilities.mixers[0]
        self._applying_preset = True
        self.multiplier_control.set_value(f"{multiplier}×")
        self.mixer_combo.setCurrentText(MIXER_LABELS[mixer])
        self.blur_slider.setValue(100)
        self.blur_label.setText("100%")
        self._applying_preset = False
        for job in self.jobs.values():
            self._update_row(job)
        self._update_diagnostics()

    def _qp_changed(self, value: int) -> None:
        self.qp_label.setText(f"QP {value}")
        self._settings_changed()

    def _blur_changed(self, value: int) -> None:
        snapped = round(value / 5) * 5
        if snapped != value:
            self.blur_slider.setValue(snapped)
            return
        self.blur_label.setText(f"{snapped}%")
        self._settings_changed()

    def _output_mode_changed(self, *_args) -> None:
        self._sync_output_controls()
        self._settings_changed()

    def _sync_output_controls(self) -> None:
        enabled = not self.same_output_check.isChecked() and not (self.renderer and self.renderer.running)
        self.output_entry.setEnabled(enabled)
        self.output_browse.setEnabled(enabled)

    def _pick_output_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if selected:
            self.output_entry.setText(selected)

    def _device_changed(self, _value: str) -> None:
        if self.renderer and self.renderer.running:
            return
        self._start_capability_check()
        self._settings_changed()

    def _collect_settings(self) -> RenderSettings:
        return RenderSettings(
            multiplier=int(self.multiplier_control.value().rstrip("×")),
            performance=self.performance_combo.currentText().lower(),
            grid=int(self.grid_combo.currentText()),
            frame_mixer=MIXERS_BY_LABEL.get(self.mixer_combo.currentText(), "linear"),
            blur_amount=self.blur_slider.value() / 100,
            video_codec=CODECS_BY_LABEL.get(self.codec_combo.currentText(), "h264"),
            qp=self.qp_slider.value(),
            parallel_jobs=int(self.parallel_control.value()),
            auto_mp4=self.auto_mp4_check.isChecked(),
            keep_ts=self.keep_ts_check.isChecked(),
            output_same_as_source=self.same_output_check.isChecked(),
            output_directory=self.output_entry.text().strip(),
            appearance=self.appearance,
            device_index=int(self.device_combo.currentText()),
            advanced_open=self.advanced_visible,
        ).validate()

    def _toggle_advanced(self, force: bool | None = None) -> None:
        show = not self.advanced_visible if force is None else force
        self.advanced_visible = show
        self.advanced_button.blockSignals(True)
        self.advanced_button.setChecked(show)
        self.advanced_button.setText("Advanced  ▴" if show else "Advanced  ▾")
        self.advanced_button.blockSignals(False)
        self.advanced_frame.setVisible(show)

    def _toggle_log(self) -> None:
        self.log_visible = not self.log_visible
        self.log_box.setVisible(self.log_visible)
        self.log_toggle.setText("Hide render log  ▴" if self.log_visible else "Show render log  ▾")

    def _append_log(self, level: str, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {level}: {message}"
        self.logger.log(logging.ERROR if level == "ERROR" else logging.WARNING if level == "WARNING" else logging.INFO, message)
        if hasattr(self, "log_box"):
            self.log_box.appendPlainText(line)

    def _update_diagnostics(self, exact_command: str | None = None, exact_filter: str | None = None) -> None:
        if not hasattr(self, "diagnostics_box"):
            return
        job = self._selected_job() if hasattr(self, "tree") else None
        settings = self._collect_settings()
        source_fps = job.probe.fps_rational if job and job.probe else "—"
        generated = f"{source_fps} × {settings.multiplier}" if job and job.probe else "—"
        active_filter = exact_filter or (filter_chain(job.probe, settings) if job and job.probe else "—")
        version = self.capabilities.version if self.capabilities else "Checking…"
        text = (
            f"FFmpeg: {self.ffmpeg or 'not found'}\nFFprobe: {self.ffprobe or 'not found'}\n"
            f"Version: {version}\nSource FPS: {source_fps}\nGenerated internal FPS: {generated}\n"
            f"Vulkan device: {settings.device_index}\nFilter: {active_filter}"
        )
        if exact_command:
            text += f"\nCommand: {exact_command}"
        self.diagnostics_box.setPlainText(text)

    def _copy_command(self) -> None:
        job = self._selected_job()
        if not job:
            QMessageBox.information(self, "Copy command", "Select a probed queue item first.")
            return
        if not job.probe or not self.ffmpeg:
            QMessageBox.information(self, "Copy command", "The selected item has not been probed yet.")
            return
        settings = self._collect_settings()
        try:
            intermediate, _ = output_paths(job.input_path, job.probe, settings)
            text = command_text(build_render_command(self.ffmpeg, job.input_path, intermediate, job.probe, settings))
        except Exception as exc:
            QMessageBox.critical(self, "Copy command", str(exc))
            return
        QApplication.clipboard().setText(text)
        self.stage_label.setText("Command copied to clipboard")

    def _open_selected_folder(self) -> None:
        job = self._selected_job()
        if job:
            self._open_path(job.output_path.parent if job.output_path else job.input_path.parent)

    def _open_path(self, path: Path) -> None:
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            QMessageBox.critical(self, "Open folder", str(exc))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._force_close:
            event.accept()
            return
        if self.renderer and self.renderer.running:
            answer = QMessageBox.question(
                self, "Exit", "Stop the active FFmpeg jobs and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._closing = True
            save_settings(self._collect_settings())
            self.renderer.stop_queue()
            self._close_deadline = time.monotonic() + 5
            event.ignore()
            QTimer.singleShot(100, self._finish_close)
            return
        save_settings(self._collect_settings())
        event.accept()

    def _finish_close(self) -> None:
        if self.renderer and self.renderer.running and time.monotonic() < self._close_deadline:
            QTimer.singleShot(100, self._finish_close)
            return
        self._force_close = True
        self.close()


def run() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("FRUC Motion Blur")
    app.setOrganizationName("Kanibal")
    app.setStyle("Fusion")
    app.setWindowIcon(app_icon())
    window = FRUCApp()
    window.show()
    raise SystemExit(app.exec())
