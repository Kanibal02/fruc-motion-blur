from __future__ import annotations

import os
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "FRUCMotionBlur"
LOG_DIR = APP_DATA / "logs"
SETTINGS_FILE = APP_DATA / "settings.json"


def find_binary(name: str) -> Path | None:
    bundled = ROOT / "ffmpeg" / "bin" / f"{name}.exe"
    if bundled.is_file():
        return bundled
    found = shutil.which(name)
    return Path(found) if found else None


def ensure_app_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
