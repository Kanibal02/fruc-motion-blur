from __future__ import annotations

import os
import unittest

from fruc_app.app import COPY, DND_FILES, FRUCApp


@unittest.skipUnless(os.name == "nt" and DND_FILES, "Windows tkinterdnd2 check")
class DragDropTests(unittest.TestCase):
    def test_customtkinter_surfaces_accept_file_drops(self) -> None:
        app = FRUCApp()
        try:
            for widget in (app.drop_zone, app.drop_zone._canvas, app.tree):
                self.assertEqual(app.tk.call("bind", widget._w, "<<DropTargetTypes>>"), "CF_HDROP")
                self.assertTrue(widget.dnd_bind("<<DropEnter>>"))
                self.assertTrue(widget.dnd_bind("<<Drop>>"))
            self.assertEqual(app._drop_action(None), COPY)
        finally:
            app.destroy()
