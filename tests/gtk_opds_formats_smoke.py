"""Original local HTTP acquisitions exercise PDF/Kindle inside the GTK Reader."""
import json
import copy
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pdf_fixture import write_pdf

output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
os.environ['TYPIX_READER_STATE'] = str(output / 'state.json')
(output / 'state.json').unlink(missing_ok=True)
from typix_reader.app import APP_ID, Gdk, GLib, ReaderApplication
from typix_reader.opds import Client
import gi
gi.require_version('Poppler', '0.18')
from gi.repository import Poppler

fixture = Path(__file__).parent / 'fixtures/kindle'
pdf = write_pdf(output / 'original-pages.pdf')
books = [
    ('PDF pages', 'application/pdf', '.pdf', pdf.read_bytes()),
    ('MOBI story', 'application/x-mobipocket-ebook', '.mobi', (fixture / 'paper-boat.mobi').read_bytes()),
    ('KF8 story', 'application/x-mobi8-ebook', '.azw3', (fixture / 'paper-boat.azw3').read_bytes()),
    ('AZW story', 'application/vnd.amazon.ebook', '.azw', (fixture / 'paper-boat.mobi').read_bytes()),
]
encrypted = Path(__file__).parent / 'fixtures/pdf/password-protected.pdf'
# Validate the encrypted fixture using real Poppler and its public test
# password. Reader's own load operation deliberately supplies no password.
unlocked = Poppler.Document.new_from_file(encrypted.resolve().as_uri(), 'reader-fixture')
if unlocked.get_n_pages() != 3:
    raise AssertionError('Original encrypted PDF must open with its known test password')
protected = ('Password PDF', 'application/pdf', '.pdf', encrypted.read_bytes())
http_books = books + [protected]
entries = ''.join(f'<entry><title>{title}</title><link rel="http://opds-spec.org/acquisition" type="{mime}" href="/get/{i}"/></entry>'
                  for i, (title, mime, _extension, _payload) in enumerate(http_books))
feed = ('<feed xmlns="http://www.w3.org/2005/Atom"><title>Original format library</title>' + entries + '</feed>').encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == '/opds':
            mime, data = 'application/atom+xml', feed
        elif self.path.startswith('/get/') and self.path[5:].isdigit() and int(self.path[5:]) < len(http_books):
            _title, mime, _extension, data = http_books[int(self.path[5:])]
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()
GLib.set_prgname(APP_ID)
app = ReaderApplication()
app.set_application_id('ai.typixdeck.reader.formatqa')
step, index = 0, 0
started = time.monotonic()
checks, errors = [], []
downloaded = None
saved_position = 0
preserved = None
kindle_query = '傍晚，我们在岸边把故事读完。'


def require(condition, message):
    if not condition:
        raise AssertionError(message)
    checks.append(message)


def ready():
    return (app.document is not None and not app.opds_busy
            and app.main_stack.get_visible_child_name() == 'reading'
            and not getattr(app, '_pdf_busy', False))


def tick():
    global step, index, downloaded, saved_position, preserved
    try:
        if time.monotonic() - started > 150:
            raise TimeoutError(f'Format integration timeout: index={index}, step={step}')
        if app.window is None:
            return True
        if step == 0:
            app.opds_client = Client(f'http://127.0.0.1:{server.server_port}/opds')
            app.show_opds()
            step = 1
        elif step == 1:
            if app.opds_busy or app.opds_feed is None:
                return True
            require(len(app.opds_feed.entries) == 5, 'All PDF and Kindle format entries appear')
            require(all(entry.acquisitions and not entry.unsupported for entry in app.opds_feed.entries), 'All supported acquisitions are selectable')
            step = 2
        elif step == 2:
            app.show_opds()
            entry = app.opds_feed.entries[index]
            app.opds_download(entry, entry.acquisitions[0])
            step = 3
        elif step == 3:
            if not ready() or app.document.path.suffix != books[index][2]:
                return True
            downloaded = app.document.path
            require(downloaded.parent == output / 'downloads', f'{books[index][2]} downloads only into isolated local library')
            require(downloaded.read_bytes() == books[index][3], f'{books[index][2]} original bytes preserved')
            require(app.window.get_window().get_state() & Gdk.WindowState.FULLSCREEN, f'{books[index][2]} reading is fullscreen')
            if index == 0:
                require(app.document.kind == 'pdf' and app.document.length == 3, 'PDF retains all original pages')
                require(app.pdf_image.get_pixbuf() is not None, 'PDF actual page pixels are rendered')
                app.show_search()
                app.search_entry.set_text('lighthouse')
                app.search_next()
                step = 4
            else:
                require(app.document.kind == 'text', f'{books[index][2]} uses the own Reader text view')
                require(any('quiet stream' in chapter.text for chapter in app.document.chapters), f'{books[index][2]} original story parsed')
                if index in (1, 2):
                    # The unique original ending lies in the second Reader
                    # chapter in both genuine MOBI7 and FDST-based KF8. Begin
                    # in the first chapter so a stale selection cannot pass.
                    if (app.document.length != 2
                            or kindle_query in app.document.chapters[0].text
                            or kindle_query not in app.document.chapters[1].text):
                        raise AssertionError('Original Kindle search fixture must span two chapters')
                    app.go_to(0)
                    app.show_search()
                    app.search_entry.set_text(kindle_query)
                    app.search_next()
                    step = 9
                else:
                    app.go_to(min(1, app.document.length - 1))
                    step = 5
        elif step == 4:
            if not ready() or app.position != 1:
                return True
            require(bool(app._pdf_cursor), 'Downloaded PDF text search reaches the second page')
            app.hide_search()
            step = 5
        elif step == 9:
            if not ready() or app._search_cursor is None:
                return True
            extension = books[index][2]
            require(app.position == 1 and app._search_cursor[0] == kindle_query,
                    f'{extension} real text search moves from the first to the second chapter')
            buffer = app.text_view.get_buffer()
            bounds = buffer.get_selection_bounds()
            require(len(bounds) == 2 and buffer.get_text(bounds[0], bounds[1], True) == kindle_query,
                    f'{extension} search highlights exactly the original matching sentence')
            require(app.text_view.get_mapped() and app.search_bar.get_visible(),
                    f'{extension} highlighted result is in the visible Reader text view')
            app.hide_search()
            step = 5
        elif step == 5:
            if not ready():
                return True
            saved_position = app.position
            app.save_progress()
            require(app.state.position_for(app.document.sha256).chapter == saved_position, f'{books[index][2]} progress saved')
            if os.environ.get('READER_QA_SCREENSHOT_COMMAND') == 'grim' and index in (0, 2):
                subprocess.run(['grim', str(output / ('reader-pdf.png' if index == 0 else 'reader-kindle.png'))], check=True, timeout=10)
            app.show_shelf()
            app.open_path(downloaded)
            step = 6
        elif step == 6:
            if not ready() or app.document.path != downloaded:
                return True
            require(app.position == saved_position, f'{books[index][2]} resumes saved page or chapter')
            index += 1
            if index == len(books):
                app.show_opds()
                preserved = (app.document, app.position, copy.deepcopy(app.state),
                             (output / 'state.json').read_bytes())
                app.info.hide()
                entry = app.opds_feed.entries[-1]
                app.opds_download(entry, entry.acquisitions[0])
                step = 7
            else:
                step = 2
        elif step == 7:
            if app.opds_busy:
                return True
            require(app.info.get_visible(), 'Encrypted OPDS PDF displays an actionable parser warning')
            warning = app.info_label.get_text()
            require('PDF' in warning and ('加密' in warning or '密码' in warning), 'Warning explains the real PDF encryption reason')
            require('未加密副本' in warning, 'Warning explains how to recover from encrypted PDF')
            require('原有图书与阅读进度保留' in warning, 'Warning confirms the existing book and progress are retained')
            require(app.document is preserved[0] and app.position == preserved[1], 'Failed OPDS parse preserves the exact open document and position')
            require(app.state == preserved[2] and (output / 'state.json').read_bytes() == preserved[3], 'Failed OPDS parse preserves in-memory and on-disk reading progress')
            require(app.main_stack.get_visible_child_name() == 'opds', 'Failed acquisition stays in the retryable OPDS view')
            require(any(path.read_bytes() == protected[3] for path in (output / 'downloads').glob('*.pdf')), 'Password PDF was actually downloaded over local HTTP before parsing')
            if os.environ.get('READER_QA_SCREENSHOT_COMMAND') == 'grim':
                subprocess.run(['grim', str(output / 'reader-encrypted-pdf-error.png')], check=True, timeout=10)
            app.opds_return()
            step = 8
        elif step == 8:
            require(ready() and app.document is preserved[0] and app.position == preserved[1], 'Returning from failed download continues the original book at its saved position')
            app.close_reader()
            return False
        return True
    except Exception as exc:
        errors.append(f'index={index}, step={step}: {exc}')
        traceback.print_exc()
        app.close_reader()
        return False


GLib.timeout_add(350, tick)
app.run(['typix-reader-format-qa'])
server.shutdown()
server.server_close()
result = {'passed': not errors, 'checks': checks, 'errors': errors}
(output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if result['passed'] else 1)
