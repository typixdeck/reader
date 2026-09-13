"""Fullscreen GTK3 reading shelf, with bounded background document loading."""
from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from .formats import Document, LoadCancelled, ReaderFormatError, load_document, read_image
from .navigation import find_next
from .state import load_state, save_state
from .opds_ui import OPDSMixin

APP_ID = "ai.typixdeck.reader"


def css_path() -> Path:
    source = Path(__file__).resolve().parent / "typix-reader.css"
    return source if source.exists() else Path("/usr/share/typix-reader/typix-reader.css")


class ReaderApplication(OPDSMixin, Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.window: Gtk.ApplicationWindow | None = None
        self.state = load_state()
        self.document: Document | None = None
        self.position = 0
        self._closed = False
        self._mapped_fullscreen = False
        self._toc_sync = False
        self._restore_offset: int | None = None
        self._restore_source = 0
        self._save_source = 0
        self._cancel = threading.Event()
        self._jobs: queue.Queue = queue.Queue(maxsize=1)
        self._search_cursor: tuple[str, int, int] | None = None
        self._write_error = False
        threading.Thread(target=self._worker, name="reader-loader", daemon=True).start()
        self.connect("shutdown", self.on_shutdown)

    def _worker(self) -> None:
        while True:
            event, work, done = self._jobs.get()
            if event.is_set():
                continue
            try:
                result, error = work(event.is_set), None
            except Exception as exc:
                result, error = None, exc
            GLib.idle_add(self._deliver, event, done, result, error)

    def _deliver(self, event, done, result, error) -> bool:
        if not self._closed and not event.is_set():
            done(result, error)
        return False

    def submit(self, work, done) -> None:
        self._cancel.set()
        self._cancel = threading.Event()
        try:
            self._jobs.get_nowait()
        except queue.Empty:
            pass
        self._jobs.put_nowait((self._cancel, work, done))

    def do_activate(self) -> None:
        if self.window is None:
            self.build_window()
            self.show_shelf()
        self.window.fullscreen()
        self.window.present()

    def do_open(self, files: list[Gio.File], _count: int, _hint: str) -> None:
        self.activate()
        if files:
            path = files[0].get_path()
            if path:
                self.open_path(Path(path))
            else:
                self.message("请选择本地文件，或通过“在线书库”浏览 Calibre OPDS。")

    def button(self, label: str, callback, tooltip: str = "") -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.connect("clicked", lambda _button: callback())
        button.set_tooltip_text(tooltip or label)
        return button

    def label(self, text: str = "", style: str = "", wrap: bool = False) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        label.set_line_wrap(wrap)
        if not wrap:
            label.set_ellipsize(Pango.EllipsizeMode.END)
        if style:
            label.get_style_context().add_class(style)
        return label

    def build_window(self) -> None:
        window = Gtk.ApplicationWindow(application=self)
        self.window = window
        window.set_title("TypixDeck 阅读器")
        window.set_default_size(800, 600)
        window.connect("key-press-event", self.on_key_press)
        window.connect("delete-event", self.on_delete)
        window.connect("map-event", self.on_map)
        provider = Gtk.CssProvider()
        provider.load_from_path(str(css_path()))
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.font_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self.font_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.apply_font()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        window.add(root)

        header = Gtk.Box(spacing=8)
        header.set_border_width(12)
        header.get_style_context().add_class("header")
        identity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.title_label = self.label("阅读器", "app-title")
        self.subtitle_label = self.label("TYPIXDECK  /  本地阅读", "muted")
        identity.pack_start(self.title_label, False, False, 0)
        identity.pack_start(self.subtitle_label, False, False, 0)
        identity.set_hexpand(True)
        identity.set_size_request(90, -1)
        header.pack_start(identity, True, True, 0)
        header.pack_start(self.button("在线书库", self.show_opds, "连接 Calibre OPDS 书库"), False, False, 0)
        header.pack_start(self.button("书架", self.show_shelf, "返回书架 · Esc"), False, False, 0)
        header.pack_start(self.button("打开文件", self.choose_file, "打开本地 TXT / Markdown / EPUB / CBZ · Ctrl+O"), False, False, 0)
        header.pack_start(self.button("返回桌面", self.close_reader, "保存进度并返回桌面 · Ctrl+Q"), False, False, 0)
        root.pack_start(header, False, False, 0)

        self.info = Gtk.InfoBar()
        self.info.set_show_close_button(True)
        self.info.set_no_show_all(True)
        self.info.connect("response", lambda *_args: self.info.hide())
        self.info_label = self.label(wrap=True)
        self.info_label.set_max_width_chars(75)
        self.info.get_content_area().add(self.info_label)
        root.pack_start(self.info, False, False, 0)

        self.main_stack = Gtk.Stack()
        self.main_stack.set_vexpand(True)
        root.pack_start(self.main_stack, True, True, 0)
        self.build_shelf()
        self.build_reading()
        self.build_opds()
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        loading.set_halign(Gtk.Align.CENTER)
        loading.set_valign(Gtk.Align.CENTER)
        self.spinner = Gtk.Spinner()
        self.spinner.set_size_request(40, 40)
        loading.pack_start(self.spinner, False, False, 0)
        loading.pack_start(self.label("正在打开本地图书…", "section-title"), False, False, 0)
        loading.pack_start(self.button("取消", self.cancel_open), False, False, 0)
        self.main_stack.add_named(loading, "loading")
        footer = Gtk.Box(spacing=8)
        footer.set_border_width(10)
        self.status_label = self.label("", "muted")
        self.status_label.set_hexpand(True)
        footer.pack_start(self.status_label, True, True, 0)
        root.pack_start(footer, False, False, 0)
        targets = [Gtk.TargetEntry.new("text/uri-list", 0, 0)]
        window.drag_dest_set(Gtk.DestDefaults.ALL, targets, Gdk.DragAction.COPY)
        window.connect("drag-data-received", self.on_drop)
        window.fullscreen()
        window.show_all()
        self.info.hide()
        self.search_bar.hide()
        self.toc_scroll.hide()
        window.fullscreen()

    def build_shelf(self) -> None:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_border_width(24)
        intro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        intro.pack_start(self.label("留一点时间，给下一页。", "shelf-title", True), False, False, 0)
        intro.pack_start(self.label("打开本地电子书，接着上次的位置继续读。", "muted", True), False, False, 0)
        box.pack_start(intro, False, False, 0)
        self.resume_button = Gtk.Button()
        self.resume_button.get_style_context().add_class("resume-card")
        self.resume_button.connect("clicked", lambda _button: self.open_path(Path(self.state.recent[0])) if self.state.recent else self.choose_file())
        self.resume_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        self.resume_box.set_border_width(16)
        self.resume_caption = self.label("开始阅读", "accent")
        self.resume_title = self.label("打开第一本书", "section-title")
        self.resume_detail = self.label("TXT · Markdown · EPUB · CBZ", "muted")
        for label in (self.resume_caption, self.resume_title, self.resume_detail):
            self.resume_box.pack_start(label, False, False, 0)
        self.resume_button.add(self.resume_box)
        box.pack_start(self.resume_button, False, False, 0)
        row = Gtk.Box(spacing=8)
        row.pack_start(self.label("最近阅读", "section-title"), True, True, 0)
        self.recent_count = self.label("0 本", "muted")
        row.pack_start(self.recent_count, False, False, 0)
        box.pack_start(row, False, False, 0)
        self.recent_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.pack_start(self.recent_list, False, False, 0)
        scroll.add(box)
        self.main_stack.add_named(scroll, "shelf")

    def build_reading(self) -> None:
        reading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toolbar = Gtk.Box(spacing=6)
        self.toolbar.set_border_width(8)
        self.toc_button = self.button("目录", self.toggle_toc, "显示 / 收起章节目录 · Ctrl+T")
        self.previous_button = self.button("上一节", lambda: self.advance(-1), "上一章节 / 图片页 · ←")
        self.next_button = self.button("下一节", lambda: self.advance(1), "下一章节 / 图片页 · →")
        self.font_less = self.button("A−", lambda: self.change_font(-2), "缩小字体 · Ctrl+−")
        self.font_more = self.button("A+", lambda: self.change_font(2), "放大字体 · Ctrl++")
        self.search_button = self.button("搜索", self.show_search, "全文搜索 · Ctrl+F；下一个 · F3")
        for button in (self.toc_button, self.previous_button, self.next_button):
            self.toolbar.pack_start(button, False, False, 0)
        self.location_label = self.label("", "muted")
        self.location_label.set_hexpand(True)
        self.toolbar.pack_start(self.location_label, True, True, 0)
        for button in (self.font_less, self.font_more, self.search_button):
            self.toolbar.pack_start(button, False, False, 0)
        reading.pack_start(self.toolbar, False, False, 0)
        self.search_bar = Gtk.Box(spacing=8)
        self.search_bar.set_border_width(8)
        self.search_bar.set_no_show_all(True)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_max_length(128)
        self.search_entry.set_placeholder_text("搜索全书文字…")
        self.search_entry.connect("activate", lambda _entry: self.search_next())
        self.search_entry.connect("search-changed", lambda _entry: setattr(self, "_search_cursor", None))
        self.search_bar.pack_start(self.search_entry, True, True, 0)
        self.search_bar.pack_start(self.button("查找下一个", self.search_next), False, False, 0)
        self.search_bar.pack_start(self.button("关闭", self.hide_search), False, False, 0)
        reading.pack_start(self.search_bar, False, False, 0)
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_position(190)
        self.paned.set_vexpand(True)
        self.toc_scroll = Gtk.ScrolledWindow()
        self.toc_scroll.set_no_show_all(True)
        self.toc_scroll.set_size_request(150, -1)
        self.toc_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.toc_store = Gtk.ListStore(str, int)
        self.toc_view = Gtk.TreeView(model=self.toc_store)
        renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END, wrap_width=-1)
        self.toc_view.append_column(Gtk.TreeViewColumn("目录", renderer, text=0))
        self.toc_view.set_headers_visible(False)
        self.toc_view.get_selection().connect("changed", self.on_toc_changed)
        self.toc_scroll.add(self.toc_view)
        self.paned.pack1(self.toc_scroll, False, False)
        self.content_stack = Gtk.Stack()
        self.content_scroll = Gtk.ScrolledWindow()
        self.content_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.content_scroll.set_overlay_scrolling(False)
        self.text_view = Gtk.TextView()
        self.text_view.set_name("book-text")
        self.text_view.set_editable(False)
        self.text_view.set_cursor_visible(False)
        self.text_view.set_left_margin(28)
        self.text_view.set_right_margin(28)
        self.text_view.set_top_margin(22)
        self.text_view.set_bottom_margin(28)
        self.text_view.set_pixels_above_lines(4)
        self.text_view.set_pixels_below_lines(4)
        self.text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.content_scroll.add(self.text_view)
        self.content_scroll.get_vadjustment().connect("value-changed", self.on_scroll)
        self.content_scroll.get_vadjustment().connect("changed", self.queue_restore)
        self.content_stack.add_named(self.content_scroll, "text")
        self.image = Gtk.Image()
        self.image.set_can_focus(True)
        self.image.set_hexpand(True)
        self.image.set_vexpand(True)
        self.content_stack.add_named(self.image, "image")
        self.content_stack.connect("size-allocate", self.on_content_size)
        self._image_size = (0, 0)
        self._image_source = 0
        self.paned.pack2(self.content_stack, True, False)
        reading.pack_start(self.paned, True, True, 0)
        self.main_stack.add_named(reading, "reading")

    def message(self, value: str, warning: bool = True) -> None:
        self.info.set_message_type(Gtk.MessageType.WARNING if warning else Gtk.MessageType.INFO)
        self.info_label.set_text(value)
        self.info.get_content_area().show_all()
        self.info.show()

    def choose_file(self) -> None:
        dialog = Gtk.FileChooserNative(title="打开本地图书", transient_for=self.window, action=Gtk.FileChooserAction.OPEN)
        dialog.set_local_only(True)
        for name, patterns in (("支持的电子书", ("*.txt", "*.md", "*.markdown", "*.epub", "*.cbz")), ("所有文件", ("*",))):
            file_filter = Gtk.FileFilter()
            file_filter.set_name(name)
            for pattern in patterns:
                file_filter.add_pattern(pattern)
                file_filter.add_pattern(pattern.upper())
            dialog.add_filter(file_filter)
        if self.state.recent:
            parent = Path(self.state.recent[0]).parent
            if parent.is_dir():
                dialog.set_current_folder(str(parent))
        response = dialog.run()
        filename = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        if filename:
            self.open_path(Path(filename))

    def on_drop(self, _window, context, _x, _y, data, _info, timestamp) -> None:
        uris = data.get_uris() or []
        path = Gio.File.new_for_uri(uris[0]).get_path() if uris else None
        Gtk.drag_finish(context, bool(path), False, timestamp)
        if path:
            self.open_path(Path(path))
        else:
            self.message("请拖入本地图书文件。")

    def book_detail(self, path: str) -> tuple[str, str]:
        item = self.state.books.get(path, {})
        saved = self.state.position_for(item.get("sha256", ""))
        title = item.get("title", Path(path).stem)
        unit = "页" if Path(path).suffix.lower() == ".cbz" else "节"
        progress = f"第 {saved.chapter + 1} / {item['length']} {unit}" if saved and item.get("length") else "点击打开"
        return title, f"{Path(path).suffix.lstrip('.').upper()}  ·  {progress}"

    def show_shelf(self) -> None:
        self.save_progress()
        self._cancel.set()
        self.document = None
        self._restore_offset = None
        self.title_label.set_text("阅读器")
        self.subtitle_label.set_text("TYPIXDECK  /  本地阅读")
        self.main_stack.set_visible_child_name("shelf")
        self.spinner.stop()
        self.status_label.set_text("Ctrl+O 打开文件  ·  Enter 打开所选图书  ·  Esc 返回桌面")
        for child in self.recent_list.get_children():
            child.destroy()
        self.recent_count.set_text(f"{len(self.state.recent)} 本")
        if self.state.recent:
            title, detail = self.book_detail(self.state.recent[0])
            self.resume_caption.set_text("继续上次阅读")
            self.resume_title.set_text(title)
            self.resume_detail.set_text(detail)
            for path in self.state.recent:
                title, detail = self.book_detail(path)
                button = Gtk.Button()
                button.get_style_context().add_class("book-row")
                row = Gtk.Box(spacing=12)
                row.set_border_width(10)
                icon = Gtk.Image.new_from_icon_name("text-x-generic-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
                row.pack_start(icon, False, False, 0)
                labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                labels.pack_start(self.label(title, "book-title"), False, False, 0)
                labels.pack_start(self.label(detail, "muted"), False, False, 0)
                row.pack_start(labels, True, True, 0)
                row.pack_start(self.label("打开  ›", "accent"), False, False, 0)
                button.add(row)
                button.set_tooltip_text(path)
                button.connect("clicked", lambda _button, value=path: self.open_path(Path(value)))
                self.recent_list.pack_start(button, False, False, 0)
        else:
            self.resume_caption.set_text("开始阅读")
            self.resume_title.set_text("打开第一本书")
            self.resume_detail.set_text("TXT · Markdown · EPUB · CBZ")
            self.recent_list.pack_start(self.label("还没有阅读记录。图书与进度只保存在这台设备上。", "muted", True), False, False, 0)
        self.recent_list.show_all()
        self.resume_button.grab_focus()

    def open_path(self, path: Path) -> None:
        self.save_progress()
        self.info.hide()
        self.main_stack.set_visible_child_name("loading")
        self.status_label.set_text("在本机解析图书；可以随时取消")
        self.spinner.start()
        self.submit(lambda cancel: load_document(path, cancel), self.document_loaded)

    def cancel_open(self) -> None:
        self._cancel.set()
        self.spinner.stop()
        if self.document:
            self.main_stack.set_visible_child_name("reading")
            self.update_location()
        else:
            self.show_shelf()

    def document_loaded(self, document: Document | None, error: Exception | None) -> None:
        self.spinner.stop()
        if error is not None:
            self.cancel_open()
            if not isinstance(error, LoadCancelled):
                detail = "文件不存在或已被移动，请用“打开文件”重新选择。" if isinstance(error, FileNotFoundError) else str(error)
                if isinstance(error, PermissionError):
                    detail = "没有读取权限，请选择当前用户可以读取的文件。"
                self.message(f"无法打开图书：{detail}")
            return
        self.document = document
        self._search_cursor = None
        self.hide_search()
        self.main_stack.set_visible_child_name("reading")
        self.title_label.set_text(document.title)
        self.subtitle_label.set_text(f"本地文件  /  {document.path.suffix.lstrip('.').upper()}")
        saved = self.state.position_for(document.sha256)
        self.position = min(max(0, saved.chapter), document.length - 1) if saved else 0
        self._toc_sync = True
        self.toc_store.clear()
        for index in range(document.length):
            self.toc_store.append([document.chapters[index].title if document.kind == "text" else f"第 {index + 1} 页", index])
        self._toc_sync = False
        for widget in (self.font_less, self.font_more, self.search_button):
            widget.set_sensitive(document.kind == "text")
        if document.kind == "text":
            self.show_chapter(self.position, saved.offset if saved else 0, save_previous=False)
        else:
            self.show_page(self.position, save_previous=False)
        self.remember(saved.offset if saved else 0)

    def select_toc(self) -> None:
        self._toc_sync = True
        self.toc_view.get_selection().select_path(Gtk.TreePath.new_from_indices([self.position]))
        self._toc_sync = False

    def toggle_toc(self) -> None:
        if self.toc_scroll.get_visible():
            self.toc_scroll.hide()
            self.text_view.grab_focus()
        else:
            for child in self.toc_scroll.get_children():
                child.show_all()
            self.toc_scroll.show()
            self.toc_view.grab_focus()

    def on_toc_changed(self, selection: Gtk.TreeSelection) -> None:
        model, tree = selection.get_selected()
        if self._toc_sync or not self.document or tree is None:
            return
        target = int(model.get_value(tree, 1))
        if target != self.position:
            self.go_to(target)

    def show_chapter(self, index: int, offset: int = 0, save_previous: bool = True) -> None:
        if not self.document or not self.document.chapters:
            return
        if save_previous:
            self.save_progress()
        self.position = max(0, min(index, self.document.length - 1))
        text = self.document.chapters[self.position].text
        self._restore_offset = min(max(0, offset), len(text))
        buffer = self.text_view.get_buffer()
        buffer.set_text(text)
        # GTK scrolls its insertion mark into view when the text widget gains
        # focus. Keep that mark at the resume location as well as the viewport,
        # or its later focus/layout pass can scroll a reopened book to the top.
        buffer.place_cursor(buffer.get_iter_at_offset(self._restore_offset or 0))
        self.content_stack.set_visible_child_name("text")
        self.select_toc()
        self.update_location()
        self.queue_restore()
        self.remember(self._restore_offset or 0)
        if not self.search_bar.get_visible() and not self.toc_scroll.get_visible():
            self.text_view.grab_focus()

    def queue_restore(self, *_args) -> None:
        if self._restore_offset is not None:
            if self._restore_source:
                GLib.source_remove(self._restore_source)
            # A newly visible GtkTextView validates long text over several
            # frames. Wait until its adjustment range stops changing.
            self._restore_source = GLib.timeout_add(100, self.restore_scroll)

    def restore_scroll(self, *_args) -> bool:
        self._restore_source = 0
        if self._restore_offset is not None and self.document and self.document.kind == "text":
            buffer = self.text_view.get_buffer()
            iterator = buffer.get_iter_at_offset(self._restore_offset)
            rectangle = self.text_view.get_iter_location(iterator)
            adjustment = self.content_scroll.get_vadjustment()
            # Text-buffer coordinates exclude TextView's top margin, whereas
            # the scroll adjustment includes it. Preserve that translation so
            # repeated reopen operations do not drift up by one paragraph.
            translation = adjustment.get_value() - self.text_view.get_visible_rect().y
            adjustment.set_value(min(max(0, rectangle.y + translation),
                                     max(0, adjustment.get_upper() - adjustment.get_page_size())))
            self._restore_offset = None
        return False

    def show_page(self, index: int, save_previous: bool = True) -> None:
        if not self.document or not self.document.images:
            return
        if save_previous:
            self.save_progress()
        self.position = max(0, min(index, self.document.length - 1))
        self.content_stack.set_visible_child_name("image")
        if not self.toc_scroll.get_visible():
            self.image.grab_focus()
        self.image.clear()
        self.select_toc()
        self.update_location()
        self.remember(0)
        width = max(240, self.content_stack.get_allocated_width() - 20)
        height = max(180, self.content_stack.get_allocated_height() - 20)
        document, page = self.document, self.position

        def decode(cancel):
            data = read_image(document, page, cancel)
            if cancel():
                raise LoadCancelled()
            loader = GdkPixbuf.PixbufLoader()
            invalid = [False]

            def prepared(value, image_width, image_height):
                invalid[0] = image_width * image_height > 64_000_000 or image_width < 1 or image_height < 1
                ratio = min(width / max(1, image_width), height / max(1, image_height), 1)
                value.set_size(1 if invalid[0] else max(1, int(image_width * ratio)),
                               1 if invalid[0] else max(1, int(image_height * ratio)))

            loader.connect("size-prepared", prepared)
            try:
                loader.write(data)
                loader.close()
                pixbuf = loader.get_pixbuf()
                if invalid[0] or pixbuf is None:
                    raise ReaderFormatError("图片尺寸超出上限（6400 万像素）")
                return pixbuf
            except GLib.Error as exc:
                try:
                    loader.close()
                except GLib.Error:
                    pass
                raise ReaderFormatError("图片损坏或缺少此格式的解码器，可继续翻页") from exc

        def decoded(pixbuf, error):
            if error:
                self.message(f"第 {page + 1} 页无法显示：{error}")
            else:
                self.image.set_from_pixbuf(pixbuf)

        self.submit(decode, decoded)

    def on_content_size(self, _widget, allocation) -> None:
        size = (allocation.width, allocation.height)
        if size == self._image_size:
            return
        self._image_size = size
        if self.document and self.document.kind == "image":
            if self._image_source:
                GLib.source_remove(self._image_source)
            self._image_source = GLib.timeout_add(180, self.resize_image)

    def resize_image(self) -> bool:
        self._image_source = 0
        if self.document and self.document.kind == "image" and self.main_stack.get_visible_child_name() == "reading":
            self.show_page(self.position, save_previous=False)
        return False

    def update_location(self) -> None:
        if not self.document:
            return
        image = self.document.kind == "image"
        self.previous_button.set_label("上一页" if image else "上一节")
        self.next_button.set_label("下一页" if image else "下一节")
        self.previous_button.set_sensitive(self.position > 0)
        self.next_button.set_sensitive(self.position < self.document.length - 1)
        self.location_label.set_text(f"{self.position + 1} / {self.document.length}")
        self.status_label.set_text("← → 翻页  ·  Esc 返回书架" if image else f"← → 翻章  ·  空格 / PgDn 向下阅读  ·  Ctrl+F 搜索  ·  字号 {self.state.font_size}")

    def go_to(self, target: int) -> None:
        if not self.document or not 0 <= target < self.document.length:
            return
        if self.document.kind == "text":
            self.show_chapter(target)
        else:
            self.show_page(target)

    def advance(self, delta: int) -> None:
        self.go_to(self.position + delta)

    def scroll_page(self, delta: int) -> None:
        if not self.document:
            return
        if self.document.kind == "image":
            self.advance(delta)
            return
        adjustment = self.content_scroll.get_vadjustment()
        maximum = max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size())
        value = adjustment.get_value()
        if delta > 0 and value >= maximum - 2:
            self.advance(1)
        elif delta < 0 and value <= adjustment.get_lower() + 2:
            self.advance(-1)
        else:
            adjustment.set_value(max(adjustment.get_lower(), min(maximum, value + delta * adjustment.get_page_size() * 0.88)))

    def current_offset(self) -> int:
        if self._restore_offset is not None:
            return self._restore_offset
        if not self.document or self.document.kind != "text":
            return 0
        rectangle = self.text_view.get_visible_rect()
        iterator, _line_top = self.text_view.get_line_at_y(rectangle.y)
        return iterator.get_offset()

    def remember(self, offset: int) -> None:
        if self.document:
            self.state.remember(self.document.path, self.document.sha256, self.position, offset,
                                self.document.title, self.document.length)
        self.write_state()

    def write_state(self) -> None:
        try:
            save_state(self.state)
            self._write_error = False
        except OSError:
            if not self._write_error and not self._closed:
                self.message("暂时无法保存进度：请检查可用空间和目录权限。仍可继续阅读，稍后操作会重试保存。")
            self._write_error = True

    def save_progress(self) -> bool:
        if self._save_source:
            GLib.source_remove(self._save_source)
            self._save_source = 0
        if self.document:
            self.remember(self.current_offset())
        return False

    def on_scroll(self, _adjustment) -> None:
        if self.document and self.document.kind == "text" and self._restore_offset is None:
            if self._save_source:
                GLib.source_remove(self._save_source)
            self._save_source = GLib.timeout_add(700, self._save_after_scroll)

    def _save_after_scroll(self) -> bool:
        self._save_source = 0
        return self.save_progress()

    def apply_font(self) -> None:
        self.font_provider.load_from_data(f"#book-text text {{ font-size: {self.state.font_size}px; }}".encode())

    def change_font(self, delta: int) -> None:
        if not self.document or self.document.kind != "text":
            return
        offset = self.current_offset()
        self.state.font_size = max(16, min(34, self.state.font_size + delta))
        self._restore_offset = offset
        self.apply_font()
        self.queue_restore()
        self.update_location()
        self.remember(offset)

    def show_search(self) -> None:
        if self.document and self.document.kind == "text":
            for child in self.search_bar.get_children():
                child.show()
            self.search_bar.show()
            self.search_entry.grab_focus()

    def hide_search(self) -> None:
        self.search_bar.hide()
        if self.document and self.document.kind == "text":
            self.text_view.grab_focus()

    def search_next(self) -> None:
        if not self.document or self.document.kind != "text":
            return
        query = self.search_entry.get_text().strip()
        if not query:
            self.show_search()
            return
        chapter, offset = self.position, self.current_offset()
        if self._search_cursor and self._search_cursor[0] == query:
            _, chapter, offset = self._search_cursor
        document = self.document
        self.status_label.set_text(f"正在搜索“{query}”…")

        def searched(match, error):
            if error:
                self.message(f"搜索未完成：{error}")
            elif self.document is document:
                self.show_match(query, match)

        self.submit(lambda cancel: find_next(document, query, chapter, offset, cancel), searched)

    def show_match(self, query, match) -> None:
        if match is None:
            self.status_label.set_text(f"未找到“{query}”")
            return
        if match.chapter != self.position:
            self.show_chapter(match.chapter, match.start)
        buffer = self.text_view.get_buffer()
        first, last = buffer.get_iter_at_offset(match.start), buffer.get_iter_at_offset(match.end)
        buffer.select_range(first, last)
        self._restore_offset = None
        self.text_view.scroll_to_mark(buffer.get_insert(), 0.12, True, 0, 0.3)
        self._search_cursor = (query, match.chapter, match.end)
        self.status_label.set_text(f"第 {match.chapter + 1} 节找到“{query}” · Enter / F3 下一个" + (" · 已回到书首" if match.wrapped else ""))

    def close_reader(self) -> None:
        self.save_progress()
        self._cancel.set()
        self.quit()

    def on_delete(self, *_args) -> bool:
        self.close_reader()
        return True

    def on_map(self, *_args) -> bool:
        if not self._mapped_fullscreen:
            self._mapped_fullscreen = True
            GLib.idle_add(self._fullscreen_after_map)
        return False

    def _fullscreen_after_map(self) -> bool:
        if not self._closed:
            self.window.fullscreen()
        return False

    def on_shutdown(self, *_args) -> None:
        self.save_progress()
        self._closed = True
        self._cancel.set()

    def on_key_press(self, _window: Gtk.Window, event: Gdk.EventKey) -> bool:
        control = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        key = event.keyval
        if control:
            actions = {Gdk.KEY_o: self.choose_file, Gdk.KEY_O: self.choose_file,
                       Gdk.KEY_f: self.show_search, Gdk.KEY_F: self.show_search,
                       Gdk.KEY_q: self.close_reader, Gdk.KEY_Q: self.close_reader,
                       Gdk.KEY_t: self.toggle_toc, Gdk.KEY_T: self.toggle_toc,
                       Gdk.KEY_plus: lambda: self.change_font(2), Gdk.KEY_equal: lambda: self.change_font(2),
                       Gdk.KEY_minus: lambda: self.change_font(-2), Gdk.KEY_KP_Add: lambda: self.change_font(2),
                       Gdk.KEY_KP_Subtract: lambda: self.change_font(-2)}
            if key in actions:
                actions[key]()
                return True
        if key == Gdk.KEY_Escape and self.main_stack.get_visible_child_name() == "opds":
            if self.opds_busy:
                self.opds_cancel_request()
            else:
                self.opds_return()
            return True
        if key == Gdk.KEY_Escape:
            if self.main_stack.get_visible_child_name() == "loading":
                self.cancel_open()
            elif self.search_bar.get_visible():
                self.hide_search()
            elif self.document:
                self.show_shelf()
            else:
                self.close_reader()
            return True
        if key == Gdk.KEY_F11:
            self.window.fullscreen()
            return True
        if key == Gdk.KEY_F3:
            self.search_next()
            return True
        if not self.document or self.main_stack.get_visible_child_name() != "reading":
            return False
        focus = self.window.get_focus()
        if isinstance(focus, (Gtk.Entry, Gtk.Button, Gtk.TreeView)) or control or event.state & Gdk.ModifierType.MOD1_MASK:
            return False
        if key in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            self.advance(-1)
            return True
        if key in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
            self.advance(1)
            return True
        if key in (Gdk.KEY_Page_Down, Gdk.KEY_KP_Page_Down, Gdk.KEY_space):
            self.scroll_page(-1 if event.state & Gdk.ModifierType.SHIFT_MASK else 1)
            return True
        if key in (Gdk.KEY_Page_Up, Gdk.KEY_KP_Page_Up):
            self.scroll_page(-1)
            return True
        return False


def main() -> int:
    GLib.set_prgname(APP_ID)
    return ReaderApplication().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
