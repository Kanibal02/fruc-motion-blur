from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from fractions import Fraction
from math import isfinite
from pathlib import Path
from uuid import uuid4


class JobStatus(str, Enum):
    WAITING = "Waiting"
    PROBING = "Probing"
    RENDERING = "Rendering"
    REMUXING = "Remuxing"
    DONE = "Done"
    CANCELLED = "Cancelled"
    FAILED = "Failed"


@dataclass(slots=True)
class ProbeInfo:
    width: int
    height: int
    fps: Fraction
    duration: float
    codec: str = "unknown"
    audio_codec: str | None = None
    pixel_format: str | None = None
    video_stream_index: int | None = None

    @property
    def fps_text(self) -> str:
        if self.fps.denominator == 1:
            return str(self.fps.numerator)
        return f"{float(self.fps):.3f}".rstrip("0").rstrip(".")

    @property
    def fps_rational(self) -> str:
        return f"{self.fps.numerator}/{self.fps.denominator}"


@dataclass(slots=True)
class RenderSettings:
    multiplier: int = 4
    performance: str = "fast"
    grid: int = 4
    frame_mixer: str = "linear"
    blur_amount: float = 1.0
    video_codec: str = "h264"
    qp: int = 28
    parallel_jobs: int = 1
    auto_mp4: bool = True
    keep_ts: bool = False
    output_same_as_source: bool = True
    output_directory: str = ""
    appearance: str = "Dark"
    device_index: int = 0
    advanced_open: bool = False
    reduced_motion: bool = False

    def validate(self) -> RenderSettings:
        defaults = RenderSettings()
        choices = {
            "multiplier": (2, 3, 4, 6, 8, 12, 16),
            "performance": ("fast", "medium", "slow"),
            "grid": (1, 2, 4),
            "frame_mixer": ("linear", "hermite"),
            "video_codec": ("h264", "hevc", "av1"),
            "appearance": ("Dark", "Light", "System"),
        }
        for name, allowed in choices.items():
            value = getattr(self, name)
            if type(value) is not type(getattr(defaults, name)) or value not in allowed:
                setattr(self, name, getattr(defaults, name))
        for name, low, high in (
            ("blur_amount", 0.25, 2.0), ("qp", 18, 40),
            ("parallel_jobs", 1, 4), ("device_index", 0, 15),
        ):
            try:
                value = float(getattr(self, name))
                if not isfinite(value):
                    raise ValueError("non-finite setting")
                value = min(high, max(low, value))
                setattr(self, name, value if name == "blur_amount" else int(value))
            except (TypeError, ValueError, OverflowError):
                setattr(self, name, getattr(defaults, name))
        for name in (
            "auto_mp4", "keep_ts", "output_same_as_source", "advanced_open", "reduced_motion",
        ):
            if not isinstance(getattr(self, name), bool):
                setattr(self, name, getattr(defaults, name))
        if not isinstance(self.output_directory, str):
            self.output_directory = ""
        return self

    @classmethod
    def from_dict(cls, values: dict[str, object]) -> RenderSettings:
        known = cls.__dataclass_fields__
        return cls(**{key: value for key, value in values.items() if key in known}).validate()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class RenderJob:
    input_path: Path
    id: str = field(default_factory=lambda: uuid4().hex)
    probe: ProbeInfo | None = None
    status: JobStatus = JobStatus.WAITING
    progress: float = 0.0
    output_path: Path | None = None
    error: str = ""
    render_multiplier: int | None = None
    stage_progress: float = 0.0

    @property
    def details(self) -> str:
        if not self.probe:
            return "Inspecting media…"
        p = self.probe
        return f"{p.width}×{p.height}  •  {p.fps_text} fps  •  {format_time(p.duration)}"


def format_time(seconds: float | None) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
