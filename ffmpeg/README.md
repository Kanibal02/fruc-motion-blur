# Bundled FFmpeg

Place the Windows builds here:

```text
ffmpeg/bin/ffmpeg.exe
ffmpeg/bin/ffprobe.exe
```

`ffplay.exe` is optional. The application prefers these bundled binaries and falls back to `PATH`.
The executables are intentionally ignored by Git because common static builds exceed GitHub's 100 MB per-file limit.
