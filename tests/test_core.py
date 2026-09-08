from __future__ import annotations

import json
import tempfile
import unittest
import subprocess
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from fruc_app.ffmpeg import (
    build_render_command,
    filter_chain,
    output_paths,
    probe_media,
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

    def test_12x_and_16x_pair_frames_before_safe_rate_mixing(self) -> None:
        for multiplier, paired_rate, mixer_count in ((12, "360000/1001", 1), (16, "480000/1001", 2)):
            with self.subTest(multiplier=multiplier):
                chain = filter_chain(self.probe, RenderSettings(multiplier=multiplier))
                self.assertIn(f"fps=source_fps*{multiplier}", chain)
                self.assertIn(f"setpts=N/(({paired_rate})*TB)", chain)
                self.assertIn("blend_vulkan=all_mode=average", chain)
                self.assertEqual(chain.count("libplacebo="), mixer_count)

    def test_16x_command_uses_complex_video_map(self) -> None:
        command = build_render_command(
            Path("ffmpeg.exe"), Path("input.mp4"), Path("out.ts"),
            self.probe, RenderSettings(multiplier=16),
        )
        self.assertIn("-filter_complex", command)
        self.assertEqual(command[command.index("-map") + 1], "[outv]")

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

    def test_hevc_and_av1_use_their_vulkan_encoder_and_container(self) -> None:
        for codec, encoder, container in (
            ("hevc", "hevc_vulkan", "mpegts"),
            ("av1", "av1_vulkan", "matroska"),
        ):
            with self.subTest(codec=codec):
                command = build_render_command(
                    Path("ffmpeg.exe"), Path("input.mp4"), Path("out"),
                    self.probe, RenderSettings(video_codec=codec),
                )
                self.assertEqual(command[command.index("-c:v") + 1], encoder)
                self.assertEqual(command[command.index("-f") + 1], container)

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

    def test_av1_uses_mkv_intermediate_and_identifiable_mp4_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "clip.mp4"
            source.touch()
            intermediate, mp4 = output_paths(
                source, self.probe, RenderSettings(video_codec="av1")
            )
            self.assertEqual(intermediate.name, "clip_FRUC4x_blur_59.94fps_AV1.temp.mkv")
            self.assertEqual(mp4.name, "clip_FRUC4x_blur_59.94fps_AV1.mp4")

    def test_progress_parses_microseconds_and_timecode(self) -> None:
        self.assertEqual(progress_seconds({"out_time_us": "2500000"}), 2.5)
        self.assertEqual(progress_seconds({"out_time": "01:02:03.500000"}), 3723.5)


class SettingsTests(unittest.TestCase):
    def test_malformed_settings_recover_per_field(self) -> None:
        settings = RenderSettings.from_dict({
            "multiplier": [], "frame_mixer": {"bad": True}, "qp": float("inf"),
            "blur_amount": "NaN", "parallel_jobs": None, "device_index": "broken",
            "auto_mp4": "false", "reduced_motion": True, "video_codec": "av1",
        })
        self.assertEqual((settings.multiplier, settings.frame_mixer, settings.qp), (4, "linear", 28))
        self.assertEqual((settings.blur_amount, settings.parallel_jobs, settings.device_index), (1.0, 1, 0))
        self.assertTrue(settings.auto_mp4)
        self.assertTrue(settings.reduced_motion)
        self.assertEqual(settings.video_codec, "av1")

    def test_non_finite_json_settings_do_not_prevent_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            path.write_text('{"qp": 1e999, "blur_amount": null, "multiplier": 8}', encoding="utf-8")
            settings = load_settings(path)
            self.assertEqual((settings.qp, settings.blur_amount, settings.multiplier), (28, 1.0, 8))

    def test_settings_round_trip_and_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            save_settings(
                RenderSettings(
                    multiplier=16, blur_amount=1.5, video_codec="av1", qp=31, parallel_jobs=3,
                    reduced_motion=True,
                ),
                path,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            data["future_setting"] = True
            path.write_text(json.dumps(data), encoding="utf-8")
            loaded = load_settings(path)
            self.assertTrue(loaded.reduced_motion)
            self.assertEqual(
                (
                    loaded.multiplier, loaded.blur_amount, loaded.video_codec,
                    loaded.qp, loaded.parallel_jobs,
                ),
                (16, 1.5, "av1", 31, 3),
            )

    def test_invalid_values_fall_back_or_clamp(self) -> None:
        settings = RenderSettings.from_dict(
            {
                "multiplier": 99, "blur_amount": 9, "video_codec": "vp9",
                "qp": 100, "parallel_jobs": 99, "device_index": -5,
            }
        )
        self.assertEqual(
            (
                settings.multiplier, settings.blur_amount, settings.video_codec,
                settings.qp, settings.parallel_jobs, settings.device_index,
            ),
            (4, 2.0, "h264", 40, 4, 0),
        )


class MetadataTests(unittest.TestCase):
    def probe(self, streams: list[dict], duration: object = "12.5") -> ProbeInfo:
        result = subprocess.CompletedProcess([], 0, json.dumps({
            "streams": streams, "format": {"duration": duration},
        }), "")
        with patch("fruc_app.ffmpeg._capture", return_value=result):
            return probe_media(Path("ffprobe"), Path("clip.mp4"))

    @staticmethod
    def video(**extra: object) -> dict:
        return {"codec_type": "video", "index": 2, "width": 1920, "height": 1080,
                "avg_frame_rate": "60000/1001", "codec_name": "h264", **extra}

    def test_unavailable_or_non_finite_stream_duration_uses_container(self) -> None:
        for duration in ("N/A", "nan", "inf", "-2", None, "0"):
            with self.subTest(duration=duration):
                self.assertEqual(self.probe([self.video(duration=duration)]).duration, 12.5)

    def test_non_finite_container_duration_is_rejected(self) -> None:
        for duration in ("N/A", "nan", "inf", "-1", "0"):
            with self.subTest(duration=duration), self.assertRaisesRegex(RuntimeError, "duration is unavailable"):
                self.probe([self.video()], duration)

    def test_cover_art_is_skipped_and_render_maps_the_probed_video(self) -> None:
        cover = self.video(index=0, disposition={"attached_pic": 1}, width=500, height=500)
        media = self.probe([cover, self.video()])
        self.assertEqual((media.width, media.video_stream_index), (1920, 2))
        command = build_render_command(Path("ffmpeg"), Path("in.mp4"), Path("out.ts"), media, RenderSettings())
        self.assertEqual(command[command.index("-map") + 1], "0:2")
        self.assertTrue(filter_chain(media, RenderSettings(multiplier=16)).startswith("[0:2]fruc_vulkan="))

    def test_cover_art_without_video_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            self.probe([self.video(disposition={"attached_pic": 1})])


if __name__ == "__main__":
    unittest.main()
