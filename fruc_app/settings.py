from __future__ import annotations

import json
from pathlib import Path

from .models import RenderSettings
from .paths import SETTINGS_FILE


def load_settings(path: Path = SETTINGS_FILE) -> RenderSettings:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RenderSettings.from_dict(data) if isinstance(data, dict) else RenderSettings()
    except (OSError, ValueError, TypeError):
        return RenderSettings()


def save_settings(settings: RenderSettings, path: Path = SETTINGS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")
    temporary.replace(path)
