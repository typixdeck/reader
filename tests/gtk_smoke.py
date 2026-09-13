"""Run on a GTK3 desktop session; uses a temporary state and sample books only.

PYTHONPATH=src python3 tests/gtk_smoke.py /tmp/reader-qa
Optional READER_QA_SCREENSHOT_COMMAND=grim saves real Wayland screenshots.
"""
import json
import os
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path

output = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/reader-qa")
output.mkdir(parents=True, exist_ok=True)
os.environ["TYPIX_READER_STATE"] = str(output / "state.json")
# Every run starts with fresh test-only history.
(output / "state.json").unlink(missing_ok=True)

from typix_reader.app import APP_ID, Gdk, GdkPixbuf, GLib, ReaderApplication

sample = output / "阅读样例.md"
sample.write_text("# 起点\n" + "在本地阅读，安静地翻过下一页。\n" * 100 + "\n# 终章\n这里是 unique target，搜索可以跨越章节。\n", encoding="utf-8")
comic = output / "漫画样例.cbz"
with zipfile.ZipFile(comic, "w") as archive:
    for index, color in ((1, 0x91B475FF), (2, 0xDCBC86FF), (10, 0x829ABFFF)):
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 600, 800)
        pixbuf.fill(color)
        path = output / f"page{index}.png"
        pixbuf.savev(str(path), "png", [], [])
        archive.write(path, path.name)

GLib.set_prgname(APP_ID)
app = ReaderApplication()
app.set_application_id("ai.typixdeck.reader.qa")
step = 0
started = time.monotonic()
checks = []
failed = []
resume_offset = 0


def require(condition, message):
    if not condition:
        raise AssertionError(message)
    checks.append(message)


def screenshot(name):
    if os.environ.get("READER_QA_SCREENSHOT_COMMAND") == "grim":
        subprocess.run(["grim", str(output / f"{name}.png")], check=True, timeout=10)


def tick():
    global step, resume_offset
    try:
        if time.monotonic() - started > 60:
            raise TimeoutError(f"GTK smoke timeout at step {step}")
        if app.window is None:
            return True
        if step == 0:
            if not app.window.get_window().get_state() & Gdk.WindowState.FULLSCREEN:
                return True
            require(app.main_stack.get_visible_child_name() == "shelf", "startup shelf is visible")
            require(bool(app.window.get_window().get_state() & Gdk.WindowState.FULLSCREEN), "window is fullscreen")
            screenshot("reader-shelf-empty")
            app.open_path(sample)
        elif step == 1:
            if not app.document or app.main_stack.get_visible_child_name() == "loading":
                return True
            require(app.document.length == 2, "Markdown headings populate two TOC entries")
            require(app.document.path == sample.resolve(), "file is opened by absolute path")
            app.toggle_toc()
            app.toc_view.get_selection().select_iter(app.toc_store.iter_nth_child(None, 1))
            require(app.position == 1, "TOC selection switches chapter without recursion")
            app.toggle_toc()
            app.go_to(0)
            app.show_search()
            app.search_entry.set_text("unique target")
            app.search_next()
        elif step == 2:
            if app.position != 1:
                return True
            require(app.position == 1, "search reaches another chapter")
            require(bool(app.text_view.get_buffer().get_selection_bounds()), "search result is selected")
            app.hide_search()
            app.change_font(2)
            require(app.state.font_size == 22, "font size control changes preference")
            app.go_to(0)
            adjustment = app.content_scroll.get_vadjustment()
            adjustment.set_value(adjustment.get_upper() / 2)
        elif step == 3:
            adjustment = app.content_scroll.get_vadjustment()
            adjustment.set_value((adjustment.get_upper() - adjustment.get_page_size()) / 2)
        elif step == 4:
            require(app.current_offset() > 0, "long chapter scroll has a nonzero character offset")
            resume_offset = app.current_offset()
            app.save_progress()
            require(app.state.position_for(app.document.sha256).offset == resume_offset, "character offset is saved")
            screenshot("reader-reading")
            app.show_shelf()
        elif step == 5:
            require(len(app.recent_list.get_children()) == 1, "recent shelf has an actionable book row")
            screenshot("reader-shelf-recent")
            app.open_path(sample)
        elif step == 6:
            if not app.document or app.main_stack.get_visible_child_name() == "loading":
                return True
        elif step == 7:
            require(app.position == 0 and app.current_offset() > 0, "reopening restores chapter and scroll location")
            require(app.current_offset() == resume_offset, "reopening preserves the exact paragraph without upward drift")
            app.open_path(comic)
        elif step == 8:
            if not app.document or app.document.kind != "image" or app.image.get_pixbuf() is None:
                return True
            require(app.document.images == ("page1.png", "page2.png", "page10.png"), "CBZ natural page order")
            app.go_to(2)
        elif step == 9:
            if app.image.get_pixbuf() is None:
                return True
            require(app.position == 2, "CBZ page navigation reaches page three")
            pixbuf = app.image.get_pixbuf()
            require(pixbuf.get_height() <= app.content_stack.get_allocated_height(), "comic page fits viewport height")
            screenshot("reader-comic")
            app.show_shelf()
            app.open_path(comic)
        elif step == 10:
            if not app.document or app.document.kind != "image" or app.image.get_pixbuf() is None:
                return True
            require(app.position == 2, "CBZ last page resumes correctly")
            app.open_path(output / "missing.txt")
        elif step == 11:
            if app.main_stack.get_visible_child_name() == "loading":
                return True
            require(app.info.get_visible(), "missing file shows a friendly recoverable message")
            require(app.document is not None and app.document.kind == "image", "failed open preserves current book")
            app.open_path(sample)
            app.cancel_open()
        elif step == 12:
            require(app.main_stack.get_visible_child_name() == "reading", "cancelled open returns to current book")
            require(app.document.kind == "image", "cancelled callback cannot replace current book")
            app.close_reader()
            return False
        step += 1
        return True
    except Exception as exc:
        failed.append(f"step {step}: {exc}")
        traceback.print_exc()
        app.close_reader()
        return False


GLib.timeout_add(400, tick)
app.run(["typix-reader-qa"])
result = {"passed": not failed, "checks": checks, "failures": failed, "state": str(output / "state.json")}
(output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(bool(failed))
