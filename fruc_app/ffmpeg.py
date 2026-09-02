from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable

from .models import ProbeInfo, RenderSettings


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".mts", ".ts", ".vob", ".webm", ".wmv",
}
COPYABLE_AUDIO = {"aac", "ac3", "eac3", "mp3"}
MIXER_CANDIDATES = ("linear", "hermite", "oversample")


@dataclass(slots=True)
class Capabilities:
    version: str
    fruc_vulkan: bool
    libplacebo: bool
    h264_vulkan: bool
    vulkan_device: bool
    pipeline: bool
    mixers: tuple[str, ...]

    @property
    def missing(self) -> list[str]:
        checks = {
            "fruc_vulkan filter": self.fruc_vulkan,
            "libplacebo filter": self.libplacebo,
            "h264_vulkan encoder": self.h264_vulkan,
            "Vulkan device": self.vulkan_device,
            "Vulkan FRUC/encode pipeline": self.pipeline,
        }
        return [name for name, available in checks.items() if not available]

    @property
    def ready(self) -> bool:
        return not self.missing and bool(self.mixers)


def parse_fps(value: str | None) -> Fraction:
    if not value or value in {"0/0", "N/A"}:
        raise ValueError("missing frame rate")
    fps = Fraction(value)
    if fps <= 0 or fps > 2000:
        raise ValueError(f"invalid frame rate: {value}")
    return fps


def select_fps(avg_frame_rate: str | None, real_frame_rate: str | None) -> Fraction:
    for value in (avg_frame_rate, real_frame_rate):
        try:
            return parse_fps(value)
        except (ValueError, ZeroDivisionError):
            pass
    raise ValueError("FFprobe did not report a usable frame rate")


def _capture(command: list[str], timeout: float = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )


def probe_media(ffprobe: Path, input_path: Path) -> ProbeInfo:
    command = [
        str(ffprobe), "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(input_path),
    ]
    result = _capture(command, 30)
    if result.returncode:
        message = result.stderr.strip() or "ffprobe failed"
        raise RuntimeError(message.splitlines()[-1])
    try:
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        duration = float(video.get("duration") or data.get("format", {}).get("duration") or 0)
        if duration <= 0:
            raise ValueError("duration is unavailable")
        return ProbeInfo(
            width=int(video["width"]),
            height=int(video["height"]),
            fps=select_fps(video.get("avg_frame_rate"), video.get("r_frame_rate")),
            duration=duration,
            codec=str(video.get("codec_name") or "unknown"),
            audio_codec=str(audio.get("codec_name")) if audio else None,
            pixel_format=video.get("pix_fmt"),
        )
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise RuntimeError(f"Unsupported or incomplete video metadata: {exc}") from exc


def _tool_text(ffmpeg: Path, *args: str) -> str:
    result = _capture([str(ffmpeg), "-hide_banner", *args])
    return f"{result.stdout}\n{result.stderr}"


def _validate_vulkan(ffmpeg: Path, device_index: int, mixer: str | None = None) -> bool:
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error",
        "-init_hw_device", f"vulkan=vk:{device_index}", "-filter_hw_device", "vk",
        "-f", "lavfi", "-i", "color=size=64x64:rate=60:duration=0.05",
    ]
    if mixer:
        command += ["-vf", f"format=nv12,hwupload,libplacebo=fps=60:frame_mixer={mixer}"]
    command += ["-f", "null", "-"]
    try:
        return _capture(command, 15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _validate_pipeline(ffmpeg: Path, device_index: int, mixer: str) -> bool:
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-init_hw_device", f"vulkan=vk:{device_index}", "-filter_hw_device", "vk",
        "-f", "lavfi", "-i", "color=size=320x180:rate=30:duration=0.05",
        "-vf", (
            "format=nv12,hwupload,"
            f"fruc_vulkan=fps=source_fps*2:perf=fast:grid=4,libplacebo=fps=30:frame_mixer={mixer}"
        ),
        "-c:v", "h264_vulkan", "-qp", "34", "-f", "null", "-",
    ]
    try:
        return _capture(command, 20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def detect_capabilities(ffmpeg: Path, device_index: int = 0) -> Capabilities:
    version_text = _tool_text(ffmpeg, "-version")
    filters = _tool_text(ffmpeg, "-filters")
    encoders = _tool_text(ffmpeg, "-encoders")
    version = next((line.strip() for line in version_text.splitlines() if line.startswith("ffmpeg version")), "Unknown")
    has_fruc = " fruc_vulkan " in filters
    has_libplacebo = " libplacebo " in filters
    has_encoder = " h264_vulkan " in encoders
    vulkan = _validate_vulkan(ffmpeg, device_index)
    mixers = tuple(
        mixer for mixer in MIXER_CANDIDATES
        if has_libplacebo and vulkan and _validate_vulkan(ffmpeg, device_index, mixer)
    )
    pipeline = bool(
        has_fruc and has_libplacebo and has_encoder and vulkan and mixers
        and _validate_pipeline(ffmpeg, device_index, mixers[0])
    )
    return Capabilities(version, has_fruc, has_libplacebo, has_encoder, vulkan, pipeline, mixers)


def fps_filename_text(fps: Fraction) -> str:
    if fps.denominator == 1:
        return str(fps.numerator)
    return f"{float(fps):.2f}".rstrip("0").rstrip(".")


def filter_chain(probe: ProbeInfo, settings: RenderSettings) -> str:
    return (
        f"fruc_vulkan=fps=source_fps*{settings.multiplier}:perf={settings.performance}:grid={settings.grid},"
        f"libplacebo=fps={probe.fps_rational}:frame_mixer={settings.frame_mixer}"
    )


def output_paths(input_path: Path, probe: ProbeInfo, settings: RenderSettings) -> tuple[Path, Path | None]:
    directory = input_path.parent if settings.output_same_as_source else Path(settings.output_directory)
    stem = f"{input_path.stem}_FRUC{settings.multiplier}x_blur_{fps_filename_text(probe.fps)}fps"
    for suffix in range(10000):
        tag = "" if suffix == 0 else f" ({suffix})"
        base = directory / f"{stem}{tag}"
        mp4 = Path(f"{base}.mp4") if settings.auto_mp4 else None
        ts = Path(f"{base}.ts") if settings.keep_ts or not settings.auto_mp4 else Path(f"{base}.temp.ts")
        if not ts.exists() and (mp4 is None or not mp4.exists()) and ts.resolve() != input_path.resolve():
            return ts, mp4
    raise RuntimeError("Could not find an available output filename")


def build_render_command(
    ffmpeg: Path,
    input_path: Path,
    ts_path: Path,
    probe: ProbeInfo,
    settings: RenderSettings,
) -> list[str]:
    audio = ["-c:a", "copy"] if probe.audio_codec in COPYABLE_AUDIO else ["-c:a", "aac", "-b:a", "320k"]
    return [
        str(ffmpeg), "-y", "-benchmark",
        "-init_hw_device", f"vulkan=vk:{settings.device_index}",
        "-filter_hw_device", "vk",
        "-hwaccel", "vulkan", "-hwaccel_output_format", "vulkan",
        "-i", str(input_path),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", filter_chain(probe, settings),
        "-c:v", "h264_vulkan", "-qp", str(settings.qp),
        *audio,
        "-progress", "pipe:1", "-nostats", "-stats_period", "0.25",
        "-f", "mpegts", str(ts_path),
    ]


def build_remux_command(ffmpeg: Path, ts_path: Path, mp4_path: Path) -> list[str]:
    return [
        str(ffmpeg), "-y", "-i", str(ts_path),
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
        "-stats_period", "0.25", str(mp4_path),
    ]


def parse_progress(lines: Iterable[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def progress_seconds(values: dict[str, str]) -> float:
    if values.get("out_time_us", "").lstrip("-").isdigit():
        return max(0.0, int(values["out_time_us"]) / 1_000_000)
    value = values.get("out_time", "0")
    try:
        hours, minutes, seconds = value.split(":")
        return max(0.0, int(hours) * 3600 + int(minutes) * 60 + float(seconds))
    except (ValueError, TypeError):
        return 0.0


def command_text(command: list[str]) -> str:
    return subprocess.list2cmdline(command)
