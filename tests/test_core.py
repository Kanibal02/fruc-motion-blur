from __future__ import annotations

import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from fruc_app.ffmpeg import (
    build_render_command,
    filter_chain,
    output_paths,
    progress_seconds,
    select_fps,
)
from fruc_app.models import ProbeInfo, RenderSettings
from fruc_app.settings import load_settings, save_settings


class FFMpegCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.probe = ProbeInfo(1920, 1080, Fraction(60000, 1001), 12.5, "h264", "aac", "yuv420p")

    def test_avg_fps_is_preferred_and_rational_is_preserved(self) -> None:
        self.assertEqual(select_fps("60000/1001", "60/1"), Fraction(60000, 1001))

    def test_fps_falls_back_to_real_rate(self) -> None:
        self.assertEqual(select_fps("0/0", "24000/1001"), Fraction(24000, 1001))

    def test_filter_chain_is_exactly_fruc_then_mixer(self) -> None:
        settings = RenderSettings(multiplier=4, performance="fast", grid=4, frame_mixer="linear")
        self.assertEqual(
            filter_chain(self.probe, settings),
            "fruc_vulkan=fps=source_fps*4:perf=fast:grid=4,libplacebo=fps=60000/1001:frame_mixer=linear",
        )

    def test_8x_uses_two_mixing_stages(self) -> None:
        settings = RenderSettings(multiplier=8, performance="fast", grid=4, frame_mixer="linear")
        self.assertEqual(
            filter_chain(self.probe, settings),
            "fruc_vulkan=fps=source_fps*8:perf=fast:grid=4,"
            "libplacebo=fps=120000/1001:frame_mixer=linear,"
            "libplacebo=fps=60000/1001:frame_mixer=linear",
        )

    def test_12x_and_16x_use_balanced_mixing_stages(self) -> None:
        for multiplier, midpoint in ((12, "180000/1001"), (16, "240000/1001")):
            with self.subTest(multiplier=multiplier):
                chain = filter_chain(self.probe, RenderSettings(multiplier=multiplier))
                self.assertIn(f"fps=source_fps*{multiplier}", chain)
                self.assertIn(f"libplacebo=fps={midpoint}", chain)
                self.assertEqual(chain.count("libplacebo="), 2)

    def test_blur_amount_uses_custom_temporal_kernel(self) -> None:
        chain = filter_chain(self.probe, RenderSettings(multiplier=4, blur_amount=1.5))
        self.assertIn("frame_mixer=custom\\\\:frame_mixer_preset=linear", chain)
        self.assertIn("frame_mixer_blur=1.5", chain)

    def test_command_initializes_vulkan_before_input_and_stays_hardware_native(self) -> None:
        command = build_render_command(Path("ffmpeg.exe"), Path("input.mp4"), Path("out.ts"), self.probe, RenderSettings())
        self.assertLess(command.index("-init_hw_device"), command.index("-i"))
        self.assertEqual(command[command.index("-hwaccel_output_format") + 1], "vulkan")
        self.assertEqual(command[command.index("-c:v") + 1], "h264_vulkan")
        self.assertNotIn("hwdownload", " ".join(command))

    def test_incompatible_audio_uses_aac(self) -> None:
        probe = ProbeInfo(640, 360, Fraction(30), 1, audio_codec="opus")
        command = build_render_command(Path("ffmpeg.exe"), Path("in.webm"), Path("out.ts"), probe, RenderSettings())
        self.assertEqual(command[command.index("-c:a") + 1], "aac")

    def test_output_name_avoids_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clip.mp4"
            source.touch()
            settings = RenderSettings(keep_ts=False, auto_mp4=True)
            ts, mp4 = output_paths(source, self.probe, settings)
            self.assertEqual(ts.name, "clip_FRUC4x_blur_59.94fps.temp.ts")
            self.assertEqual(mp4.name, "clip_FRUC4x_blur_59.94fps.mp4")
            mp4.touch()
            next_ts, next_mp4 = output_paths(source, self.probe, settings)
            self.assertIn("(1)", next_ts.name)
            self.assertIn("(1)", next_mp4.name)

    def test_progress_parses_microseconds_and_timecode(self) -> None:
        self.assertEqual(progress_seconds({"out_time_us": "2500000"}), 2.5)
        self.assertEqual(progress_seconds({"out_time": "01:02:03.500000"}), 3723.5)


class SettingsTests(unittest.TestCase):
    def test_settings_round_trip_and_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            save_settings(RenderSettings(multiplier=16, blur_amount=1.5, qp=31), path)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["future_setting"] = True
            path.write_text(json.dumps(data), encoding="utf-8")
            loaded = load_settings(path)
            self.assertEqual((loaded.multiplier, loaded.blur_amount, loaded.qp), (16, 1.5, 31))

    def test_invalid_values_fall_back_or_clamp(self) -> None:
        settings = RenderSettings.from_dict({"multiplier": 99, "blur_amount": 9, "qp": 100, "device_index": -5})
        self.assertEqual((settings.multiplier, settings.blur_amount, settings.qp, settings.device_index), (4, 2.0, 40, 0))


if __name__ == "__main__":
    unittest.main()
