"""Give Qt's Windows offscreen plugin the same fonts as the native desktop."""
import os
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase, QRawFont
from PySide6.QtWidgets import QApplication


def load_test_fonts() -> None:
    if os.name == "nt" and QApplication.platformName() == "offscreen":
        directory = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf", "segoeuil.ttf", "seguisym.ttf"):
            path = directory / name
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
    font = QFont("Segoe UI", 10)
    if not QRawFont.fromFont(font).supportsCharacter(ord("A")):
        raise RuntimeError("Qt has no usable text font; UI captures would be invalid")
