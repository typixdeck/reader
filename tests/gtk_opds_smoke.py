"""Controlled local HTTP + GTK OPDS flow; never contacts or reads user libraries."""
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from http.server import ThreadingHTTPServer
from pathlib import Path

output = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/reader-opds-qa")
output.mkdir(parents=True, exist_ok=True)
os.environ["TYPIX_READER_STATE"] = str(output / "state.json")
(output / "state.json").unlink(missing_ok=True)
from test_opds import Handler
from typix_reader.app import APP_ID, Gdk, GLib, Gtk, ReaderApplication

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"
old_book = output / "原有图书.txt"
old_book.write_text("原有图书的阅读内容。\n" * 80, encoding="utf-8")
GLib.set_prgname(APP_ID)
app = ReaderApplication()
app.set_application_id("ai.typixdeck.reader.opdsqa")
step = 0
started = time.monotonic()
checks, errors = [], []
old_document = None


def require(value, message):
    if not value:
        raise AssertionError(message)
    checks.append(message)


def screenshot(name):
    if os.environ.get("READER_QA_SCREENSHOT_COMMAND") == "grim":
        subprocess.run(["grim", str(output / (name + ".png"))], check=True, timeout=10)


def login_dialog():
    for dialog in Gtk.Window.list_toplevels():
        if isinstance(dialog, Gtk.Dialog) and dialog.get_title() == "连接 Calibre / OPDS":
            entries = [widget for widget in dialog.get_content_area().get_children() if isinstance(widget, Gtk.Entry)]
            if len(entries) == 3:
                entries[0].set_text(base + "/basic/opds")
                entries[1].set_text("user")
                entries[2].set_text("secret")
                require(not entries[2].get_visibility(), "login password field is masked")
                dialog.response(Gtk.ResponseType.OK)
                return False
    return True


def tick():
    global step, old_document
    try:
        if time.monotonic() - started > 65:
            raise TimeoutError(f"OPDS GTK timeout at step {step}")
        if app.window is None:
            return True
        if step == 0:
            app.open_path(old_book)
        elif step == 1:
            if not app.document:
                return True
            old_document = app.document
            GLib.timeout_add(100, login_dialog)
            app.show_opds()
        elif step == 2:
            if app.opds_busy or app.opds_feed is None:
                return True
            require(app.document is old_document, "opening authenticated OPDS preserves current book")
            require(len(app.opds_list.get_children()) == 3, "authenticated navigation/acquisition rows appear")
            require(app.opds_next.get_sensitive(), "pagination action is available")
            require(app.window.get_window().get_state() & Gdk.WindowState.FULLSCREEN, "OPDS view remains fullscreen")
            for widget in (app.opds_previous, app.opds_next, app.opds_cancel, app.opds_query):
                coordinates = widget.translate_coordinates(app.window, 0, 0)
                require(coordinates[1] + widget.get_allocated_height() <= app.window.get_allocated_height(), "OPDS control fits viewport")
            screenshot("reader-opds-library")
            app.opds_activate(app.opds_feed.entries[0])
        elif step == 3:
            if app.opds_busy:
                return True
            require(len(app.opds_history) == 1 and app.opds_back.get_sensitive(), "navigation creates a reachable breadcrumb")
            app.opds_browse(app.opds_feed.next)
        elif step == 4:
            if app.opds_busy:
                return True
            require("page=2" in app.opds_feed.url, "next page follows server link")
            app.opds_query.set_text("local title")
            app.opds_search()
        elif step == 5:
            if app.opds_busy:
                return True
            require("query=local%20title" in app.opds_feed.url, "Calibre query template drives search")
            app.opds_browse(base + "/slow-header")
            GLib.timeout_add(150, lambda: (app.opds_cancel_request(), False)[1])
        elif step == 6:
            if app.opds_busy:
                return True
            require(app.document is old_document, "cancelling slow HTTP headers preserves previous book")
            app.opds_return()
            require(app.main_stack.get_visible_child_name() == "reading", "Esc-equivalent returns to previous reading view")
            app.show_opds()
            entry = app.opds_feed.entries[1]
            app.opds_download(entry, entry.acquisitions[0])
        elif step == 7:
            if app.opds_busy or app.main_stack.get_visible_child_name() != "reading":
                return True
            require(app.document is not old_document, "acquired book opens with existing local parser")
            require(app.document.path.parent == output / "downloads", "download is stored only in isolated local library")
            require("local reading fixture" in app.document.chapters[0].text, "downloaded text is rendered")
            require(old_book.read_text(encoding="utf-8").startswith("原有图书"), "previous book bytes remain untouched")
            saved = (output / "state.json").read_text()
            require("secret" not in saved and "Authorization" not in saved, "reading state contains no network credentials")
            screenshot("reader-opds-downloaded")
            app.close_reader()
            return False
        step += 1
        return True
    except Exception as exc:
        errors.append(f"step {step}: {exc}")
        traceback.print_exc()
        app.close_reader()
        return False


GLib.timeout_add(350, tick)
app.run(["typix-reader-opds-qa"])
server.shutdown()
server.server_close()
result = {"passed": not errors, "checks": checks, "errors": errors}
(output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if result["passed"] else 1)
