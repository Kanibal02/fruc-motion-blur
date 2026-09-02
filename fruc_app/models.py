from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from fractions import Fraction
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
    qp: int = 28
    auto_mp4: bool = True
    keep_ts: bool = False
    output_same_as_source: bool = True
    output_directory: str = ""
    appearance: str = "Dark"
    device_index: int = 0
    advanced_open: bool = False

    def validate(self) -> RenderSettings:
        self.multiplier = self.multiplier if self.multiplier in {2, 3, 4, 6, 8} else 4
        self.performance = self.performance if self.performance in {"fast", "medium", "slow"} else "fast"
        self.grid = self.grid if self.grid in {1, 2, 4} else 4
        self.frame_mixer = self.frame_mixer if self.frame_mixer else "linear"
        self.qp = min(40, max(18, int(self.qp)))
        self.appearance = self.appearance if self.appearance in {"Dark", "Light", "System"} else "Dark"
        self.device_index = min(15, max(0, int(self.device_index)))
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
