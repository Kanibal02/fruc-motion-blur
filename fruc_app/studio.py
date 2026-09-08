"""Painted studio controls. All visuals are native Qt and require no image assets."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import (
    QApplication, QPushButton, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget,
)

from .animation import HighRefreshTween, motion_enabled


DETAIL_ROLE = int(Qt.ItemDataRole.UserRole) + 1
STATUS_ROLE = DETAIL_ROLE + 1
PROGRESS_ROLE = DETAIL_ROLE + 2


def translucent(color: str, alpha: int) -> QColor:
    result = QColor(color)
    result.setAlpha(alpha)
    return result


class MotionArtwork(QWidget):
    """An illustrative exposure study, never a simulated video-processing preview."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFixedWidth(224)
        self.setMinimumHeight(148)
        self.colors: dict[str, str] = {}
        self.samples = 4
        self.blur = 1.0
        self.busy = False
        self.phase = 0.0
        self._started = time.perf_counter()
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        self.setToolTip("An illustration of temporal sampling, not a preview of your video")

    def set_colors(self, colors: dict[str, str]) -> None:
        self.colors = colors
        self.update()

    def set_sampling(self, samples: int, blur: float) -> None:
        self.samples, self.blur = samples, blur
        self.update()

    def sync_activity(self) -> None:
        active = (
            motion_enabled() and not self.busy and self.isVisible()
            and self.window().isActiveWindow() and not self.window().isMinimized()
        )
        if active:
            self.timer.start()
        else:
            self.timer.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.sync_activity()

    def hideEvent(self, event) -> None:
        self.timer.stop()
        super().hideEvent(event)

    def _tick(self) -> None:
        self.phase = (time.perf_counter() - self._started) * 0.75
        self.update()

    def paintEvent(self, event) -> None:
        if not self.colors:
            return
        c = self.colors
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        glow = QRadialGradient(w * 0.52, h * 0.48, w * 0.52)
        glow.setColorAt(0, translucent(c["accent"], 40))
        glow.setColorAt(1, translucent(c["accent"], 0))
        p.fillRect(self.rect(), glow)
        p.setPen(QPen(translucent(c["muted"], 25), 1))
        for x in range(12, w, 24):
            for y in range(25, h - 18, 24):
                p.drawPoint(x, y)
        p.save()
        p.translate(w * 0.52 + math.sin(self.phase) * 7, h * 0.47)
        p.rotate(-24)
        count = min(16, self.samples + 3)
        spacing = 8 + self.blur * 3
        for index in range(count):
            strength = (index + 1) / count
            x = (index - (count - 1) / 2) * spacing
            gradient = QLinearGradient(x, -39, x + 26, 39)
            gradient.setColorAt(0, translucent(c["accent"], round(35 + strength * 175)))
            gradient.setColorAt(1, translucent(c["cyan"], round(20 + strength * 195)))
            p.setBrush(gradient)
            p.setPen(QPen(translucent(c["cyan"] if index == count - 1 else c["accent"], round(35 + strength * 180)), 1))
            p.drawRoundedRect(QRectF(x - 13, -36, 26, 72), 8, 8)
        p.restore()
        p.setPen(QColor(c["muted"]))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        p.drawText(QRectF(0, h - 26, w, 20), Qt.AlignmentFlag.AlignCenter, f"MOTION STUDY   /   {self.samples:02d}×")
        p.end()


class PresetButton(QPushButton):
    def __init__(self, label: str, samples: int, mixer: str, parent: QWidget) -> None:
        super().__init__(label, parent)
        self.samples, self.mixer = samples, mixer
        self.colors: dict[str, str] = {}
        self.strength = 0.0
        self.setCheckable(True)
        self.setMinimumWidth(64)
        self.setFixedHeight(77)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{samples}× sampling · {mixer.title()} mixer · 100% blur")
        self.setAccessibleName(f"{label} preset, {samples} times sampling, {mixer} mixer")
        self.tween = HighRefreshTween(self, self._hover)

    def set_colors(self, colors: dict[str, str]) -> None:
        self.colors = colors
        self.update()

    def _hover(self, value: float) -> None:
        self.strength = value
        self.update()

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        if self.isEnabled():
            self.tween.start(self.strength, 1, 140)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.tween.start(self.strength, 0, 180)

    def paintEvent(self, event) -> None:
        if not self.colors:
            return
        c = self.colors
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(1 if self.isEnabled() else 0.45)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QPen(QColor(c["accent"] if self.isChecked() or self.hasFocus() else c["border"]), 1.2))
        p.setBrush(QColor(c["raised"] if self.isChecked() else c["field"]))
        p.drawRoundedRect(rect, 10, 10)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(translucent(c["accent"], round(15 + self.strength * 22) if self.isChecked() else round(self.strength * 20)))
        p.drawRoundedRect(rect, 10, 10)
        for index in range(5):
            alpha = 230 if self.isChecked() else 155
            p.setPen(QPen(translucent(c["cyan"] if self.mixer == "hermite" else c["accent"], round(alpha * (index + 1) / 5)), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            x = self.width() / 2 - 14 + index * 7
            y = 16 + (2 - abs(index - 2)) * 1.5
            p.drawLine(round(x - 2), round(y + 7), round(x + 2), round(y - 2))
        p.setPen(QColor(c["text"]))
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        p.drawText(QRectF(0, 33, self.width(), 19), Qt.AlignmentFlag.AlignCenter, self.text())
        p.setPen(QColor(c["muted"]))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(0, 53, self.width(), 16), Qt.AlignmentFlag.AlignCenter, f"{self.samples}× {self.mixer.title()}")
        p.end()


class QueueDelegate(QStyledItemDelegate):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.colors: dict[str, str] = {}

    def sizeHint(self, option, index) -> QSize:
        return QSize(160, 66)

    def paint(self, painter: QPainter, option, index) -> None:
        if not self.colors:
            super().paint(painter, option, index)
            return
        c = self.colors
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text, opt.icon = "", QIcon()
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        r = QRectF(option.rect).adjusted(12, 0, -12, 0)
        painter.setPen(QPen(translucent(c["border"], 115), 1))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        if index.column() == 0:
            icon = QRectF(r.left(), r.center().y() - 18, 36, 36)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(c["raised"]))
            painter.drawRoundedRect(icon, 9, 9)
            painter.setPen(QPen(QColor(c["accent"]), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(icon.adjusted(9, 10, -9, -10), 2, 2)
            painter.drawLine(round(icon.left() + 15), round(icon.top() + 10), round(icon.left() + 15), round(icon.bottom() - 10))
            r.adjust(48, 0, 0, 0)
            painter.setPen(QColor(c["text"]))
            painter.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
            name = painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, max(0, int(r.width())))
            painter.drawText(r.adjusted(0, 11, 0, -30), Qt.AlignmentFlag.AlignVCenter, name)
            painter.setPen(QColor(c["muted"]))
            painter.setFont(QFont("Segoe UI", 9))
            detail = str(index.data(DETAIL_ROLE) or "")
            detail = painter.fontMetrics().elidedText(detail, Qt.TextElideMode.ElideRight, max(0, int(r.width())))
            painter.drawText(r.adjusted(0, 32, 0, -10), Qt.AlignmentFlag.AlignVCenter, detail)
        elif index.column() == 1:
            painter.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
            painter.setPen(QColor(c["muted"]))
            painter.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        else:
            status = str(index.data(STATUS_ROLE) or "Waiting")
            key = {"Done": "success", "Failed": "danger", "Cancelled": "warning", "Rendering": "accent", "Remuxing": "cyan", "Probing": "cyan"}.get(status, "muted")
            color = c[key]
            painter.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
            width = min(r.width(), painter.fontMetrics().horizontalAdvance(text) + 24)
            badge = QRectF(r.left(), r.center().y() - 13, width, 26)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(translucent(color, 22))
            painter.drawRoundedRect(badge, 7, 7)
            painter.setPen(QColor(color))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)
            if status in {"Rendering", "Remuxing"}:
                fraction = float(index.data(PROGRESS_ROLE) or 0)
                painter.setPen(QPen(translucent(color, 40), 2))
                painter.drawLine(round(r.left()), round(r.bottom() - 7), round(r.right()), round(r.bottom() - 7))
                painter.setPen(QPen(QColor(color), 2))
                painter.drawLine(round(r.left()), round(r.bottom() - 7), round(r.left() + r.width() * fraction), round(r.bottom() - 7))
        painter.restore()
