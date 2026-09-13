"""Verbose wrapper around the isolated GTK smoke test for scroll restoration QA."""
import runpy
import sys
from pathlib import Path

from typix_reader.app import ReaderApplication


def wrap(name):
    original = getattr(ReaderApplication, name)

    def wrapped(self, *args, **kwargs):
        before = self._restore_offset
        length = self.text_view.get_buffer().get_char_count() if hasattr(self, "text_view") else -1
        current = self.current_offset() if hasattr(self, "text_view") else -1
        print("BEFORE", name, "pending", before, "current", current, "chars", length,
              "argument", args[0] if args and isinstance(args[0], int) else "", flush=True)
        value = original(self, *args, **kwargs)
        adjustment = self.content_scroll.get_vadjustment() if hasattr(self, "content_scroll") else None
        print("AFTER", name, "pending", self._restore_offset, "current", self.current_offset() if hasattr(self, "text_view") else -1,
              "adjustment", (adjustment.get_value(), adjustment.get_upper(), adjustment.get_page_size()) if adjustment else None, flush=True)
        return value

    return wrapped


for name in ("remember", "show_chapter", "restore_scroll", "document_loaded", "show_shelf", "save_progress"):
    setattr(ReaderApplication, name, wrap(name))
runpy.run_path(str(Path(__file__).with_name("gtk_smoke.py")), run_name="__main__")
