"""Small, refresh-aware animations with one shared reduced-motion preference."""
from __future__ import annotations

import time
import weakref
from collections.abc import Callable
from math import isfinite

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget


def motion_enabled() -> bool:
    app = QApplication.instance()
    return not bool(app and app.property("reducedMotion"))


def frame_interval_ms(refresh_rate: float) -> int:
    refresh_rate = refresh_rate if isfinite(refresh_rate) and refresh_rate > 0 else 60.0
    return max(1, int(1000 / refresh_rate))


class HighRefreshTween:
    _instances: weakref.WeakSet[HighRefreshTween] = weakref.WeakSet()

    def __init__(self, owner: QWidget, update: Callable[[float], None]) -> None:
        self._owner = owner
        self._update = update
        self._timer = QTimer(owner)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._start_value = 0.0
        self.end_value = 0.0
        self._duration = 0.0
        self._started = 0.0
        self._instances.add(self)

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def start(self, start_value: float, end_value: float, duration_ms: int) -> None:
        self.stop()
        self._start_value = float(start_value)
        self.end_value = float(end_value)
        if not motion_enabled() or duration_ms <= 0 or start_value == end_value:
            self._update(self.end_value)
            return
        screen = self._owner.screen()
        self._timer.setInterval(frame_interval_ms(screen.refreshRate() if screen else 60.0))
        self._duration = max(0.001, duration_ms / 1000)
        self._started = time.perf_counter()
        self._timer.start()
        self._tick()

    def stop(self) -> None:
        self._timer.stop()

    def finish(self) -> None:
        self.stop()
        self._update(self.end_value)

    def dispose(self) -> None:
        self.stop()
        self._timer.deleteLater()
        self._instances.discard(self)

    @classmethod
    def finish_all(cls) -> None:
        for tween in list(cls._instances):
            try:
                if tween.running:
                    tween.finish()
            except RuntimeError:  # A Qt owner can be deleted before its Python wrapper.
                cls._instances.discard(tween)

    def _tick(self) -> None:
        progress = min(1.0, (time.perf_counter() - self._started) / self._duration)
        if progress >= 1:
            self.finish()
        else:
            eased = 1 - (1 - progress) ** 3
            self._update(self._start_value + (self.end_value - self._start_value) * eased)


def reveal(widget: QWidget, show: bool, height: int | None = None, animate: bool = True) -> None:
    previous = getattr(widget, "_reveal_tween", None)
    if previous:
        previous.dispose()
    start = widget.height() if widget.isVisible() else 0
    target = (height or widget.sizeHint().height()) if show else 0
    widget.setMinimumHeight(0)
    widget.setVisible(True)

    def update(value: float) -> None:
        widget.setMaximumHeight(max(0, round(value)))
        if value == target:
            widget.setVisible(show)
            if show:
                widget.setMaximumHeight(height or 16777215)

    widget._reveal_tween = HighRefreshTween(widget, update)
    widget._reveal_tween.start(start, target, 230 if animate else 0)
