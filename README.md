# FRUC Motion Blur

A native Windows desktop GUI for GPU-only Vulkan frame-rate up-conversion and temporal frame mixing with a bundled FFmpeg build. It turns ordinary video into a motion-blurred result through this hardware filter chain:

```text
fruc_vulkan=fps=source_fps*MULTIPLIER:perf=PERF:grid=GRID,
libplacebo=fps=SOURCE_FPS:frame_mixer=MIXER
```

The input is Vulkan-decoded, both filters remain on GPU, and the result is encoded with the selected H.264, H.265/HEVC, or AV1 Vulkan encoder. No CPU interpolation or `hwdownload` path is used.

## Requirements

- Windows 10/11
- Python 3.10 or newer (tested with Python 3.12)
- A Vulkan-capable GPU and driver supporting the FFmpeg filters in use
- `ffmpeg.exe` and `ffprobe.exe` in `ffmpeg/bin/`, or compatible binaries on `PATH`
- The Python packages in `requirements.txt`

Install the GUI dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Launch

Double-click `fruc_motion_blur.pyw`, or run:

```powershell
python .\fruc_motion_blur.pyw
```

Drop files or a folder onto the add area. Folder drops scan supported videos in that folder only; they are intentionally not recursive. Choose a preset or individual settings and select **Start Queue**.

The PySide6 interface keeps the console-free `.pyw` launcher while providing native drag-and-drop, dark/light/system themes, animated pickers and progress, smooth settings scrolling, and a scalable Qt layout. Mouse-wheel input over a picker or slider always scrolls the settings panel instead of changing the hovered value. Select a finished item and use **Render again** to immediately process only that file with the settings currently shown; its previous output is kept and the new render receives the usual numbered suffix.

## Studio interface

- A violet and mint palette, illustrated motion trails, one-click look cards, and a queue with two-line clip details and distinct status badges.
- Hover transitions, refresh-aware pickers and progress, and animated disclosure of the log and advanced settings.
- **Motion on / off** in the header saves a reduced-motion preference. Decorative motion pauses during rendering and when the window is inactive, hidden, or minimized. The motion study is an illustration, not a preview of the output video.
- **Ctrl+O** adds videos; **Ctrl+Enter** starts the queue. Keyboard focus remains visible, and the primary controls fit the 1060×700 minimum window.
- Completed clips retain their original sample count when the settings change. Remux progress starts at zero for its own stage, and the final summary distinguishes completed, failed, and cancelled jobs.

## Render behavior

- Source FPS comes from `avg_frame_rate`, with `r_frame_rate` as a fallback. The rational value is preserved.
- Cover-art streams are excluded when choosing the video, and the render maps the same stream that was probed. Missing or non-finite stream duration falls back to valid container duration.
- Default settings are 4× FRUC, Fast performance, grid 4, `linear` mixing, 100% blur amount, H.264 Vulkan QP 28, and one render at a time. The parallel-render control allows 1–4 simultaneous jobs; higher values share GPU, VRAM, and disk bandwidth.
- Output names follow `inputname_FRUC4x_blur_59.94fps.mp4`. Existing files are never overwritten; a numbered suffix is added.
- H.264/HEVC render through MPEG-TS; AV1 uses Matroska. When automatic MP4 remux is enabled, FFmpeg stream-copies the completed intermediate to MP4 with `+faststart`.
- AAC, AC-3, E-AC-3, and MP3 audio are copied. Other audio formats are converted to high-bitrate AAC for container compatibility.
- A failed render removes its incomplete intermediate. A failed or cancelled MP4 remux removes the incomplete MP4 but keeps the already-valid TS/MKV.
- If MP4 remux fails, the preserved TS/MKV is available through **Open output**. Launch errors also clean up reserved temporary files and process handles.
- **Cancel Active** gracefully ends current renders, remuxes completed frames to partial MP4 files when possible, and continues with waiting jobs. **Stop Queue** ends active items, preserves any usable intermediate files, and leaves remaining jobs waiting.

## Presets and mixer support

At startup the app validates Vulkan plus `fruc_vulkan`, `libplacebo`, every available Vulkan codec, and each proposed libplacebo mixer against the selected Vulkan device. Unsupported codecs and mixers are not shown. The app exposes the two useful blur mixers accepted by the supplied build:

- `linear`
- `hermite`

**Linear** is the default and produces more visible motion blur. **Hermite** produces less blur. `oversample` is intentionally hidden because its result is barely blurred, and `cubic` is rejected by the build. The exact filter and command are visible under **Advanced**.

Higher multipliers use safe GPU reductions. 8× uses staged libplacebo mixing. For 12×/16×, `blend_vulkan` first averages adjacent generated-frame pairs before libplacebo mixing; this preserves every sample while avoiding libplacebo's broken timing for streams above 1000 FPS.

**Blur amount** independently adjusts libplacebo's temporal-kernel width from 25% to 200%. The 100% default is byte-for-byte equivalent to the previous filter configuration; lower values shorten the trail and higher values lengthen it without changing the FRUC multiplier.

Custom Bezier temporal weighting, explicit shutter phase, and arbitrary sample-weight curves are not exposed because the detected FFmpeg/libplacebo interface provides only a named `frame_mixer`. Implementing those controls honestly would require a verified custom shader/filter path; a GUI slider alone would be cosmetic.

## Settings and logs

Settings are saved to:

```text
%LOCALAPPDATA%\FRUCMotionBlur\settings.json
```

The in-app log is capped. Rotating persistent logs are stored in:

```text
%LOCALAPPDATA%\FRUCMotionBlur\logs\fruc-motion-blur.log
```

FFmpeg child processes are launched without console windows. Cancellation first sends `q`, then terminates, then kills only if the process does not exit.

## Tests

Run the standard-library test suite:

```powershell
python -m unittest discover -v
```

Tests cover rational FPS handling, exact filter construction, Vulkan argument order, audio fallback, safe output naming, progress parsing, and settings persistence.

Regression tests also cover malformed settings, cover-art and duration metadata, remux recovery, process cleanup, stale GPU checks, preset clicks, reduced motion, minimum window size, and queue transitions. GitHub Actions runs the suite on Windows with Python 3.10 / Qt 6.8 at 100% scaling and Python 3.12 / current supported Qt at 150% scaling, then uploads actual Qt screenshots of empty, ready, rendering, light, compact, and advanced views. These captures use illustrative queue fixtures and do not validate GPU rendering.

To reproduce the screenshots locally without a GPU:

```powershell
python tools/capture_ui.py --output ui-captures
```

Actual `fruc_vulkan` rendering still requires the compatible FFmpeg build and a supported Windows GPU; the automated tests do not replace an end-to-end render on that hardware.

## Project structure

```text
fruc_motion_blur.pyw       Windows GUI entry point
fruc_app/app.py            PySide6 interface and main-thread event handling
fruc_app/animation.py      Refresh-aware tweens and reduced-motion handling
fruc_app/studio.py         Native painted motion study, preset cards, and queue rows
fruc_app/ffmpeg.py         Probe, capability, command, naming, and progress helpers
fruc_app/renderer.py       Parallel workers and process lifecycle
fruc_app/models.py         Jobs, settings, statuses, and media metadata
fruc_app/settings.py       JSON settings persistence
fruc_app/paths.py          Bundled binary and app-data paths
tests/test_core.py         Unit tests
ffmpeg/bin/                Local FFmpeg executables (not committed due to size)
```
