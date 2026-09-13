"""Keyboard-accessible Calibre browser integrated with the local Reader."""
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from .formats import LoadCancelled, load_document
from .opds import Client, OPDSError, load_server, save_server
from .state import state_path


class OPDSMixin:
    def build_opds(self):
        self.opds_client = None
        self.opds_feed = None
        self.opds_history = []
        self.opds_busy = False
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_border_width(16)
        toolbar = Gtk.Box(spacing=8)
        self.opds_back = self.button("上一级", self.opds_go_back)
        toolbar.pack_start(self.opds_back, False, False, 0)
        toolbar.pack_start(self.button("书库首页", lambda: self.opds_browse(self.opds_client.url, reset=True) if self.opds_client else self.opds_setup()), False, False, 0)
        toolbar.pack_start(self.button("刷新", self.opds_refresh), False, False, 0)
        toolbar.pack_end(self.button("服务器 / 登录", self.opds_setup), False, False, 0)
        root.pack_start(toolbar, False, False, 0)
        self.opds_breadcrumbs = Gtk.Box(spacing=4)
        root.pack_start(self.opds_breadcrumbs, False, False, 0)
        search = Gtk.Box(spacing=8)
        self.opds_query = Gtk.SearchEntry()
        self.opds_query.set_max_length(200)
        self.opds_query.set_placeholder_text("搜索 Calibre 图书、作者…")
        self.opds_query.connect("activate", lambda *_: self.opds_search())
        search.pack_start(self.opds_query, True, True, 0)
        self.opds_search_button = self.button("搜索书库", self.opds_search)
        search.pack_start(self.opds_search_button, False, False, 0)
        root.pack_start(search, False, False, 0)
        self.opds_list = Gtk.ListBox()
        self.opds_list.get_style_context().add_class("opds-list")
        self.opds_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.opds_list.connect("row-activated", lambda _box, row: self.opds_activate(row.entry))
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.add(self.opds_list)
        root.pack_start(scroll, True, True, 0)
        bottom = Gtk.Box(spacing=8)
        self.opds_previous = self.button("上一页", lambda: self.opds_browse(self.opds_feed.previous) if self.opds_feed else None)
        self.opds_next = self.button("下一页", lambda: self.opds_browse(self.opds_feed.next) if self.opds_feed else None)
        self.opds_cancel = self.button("取消请求", self.opds_cancel_request)
        bottom.pack_start(self.opds_previous, False, False, 0)
        bottom.pack_start(self.opds_next, False, False, 0)
        bottom.pack_end(self.opds_cancel, False, False, 0)
        root.pack_start(bottom, False, False, 0)
        self.main_stack.add_named(root, "opds")

    def show_opds(self):
        self.save_progress()
        self._cancel.set()
        self.main_stack.set_visible_child_name("opds")
        self.title_label.set_text("Calibre 书库")
        self.subtitle_label.set_text("OPDS  /  浏览并下载到本机")
        self.opds_set_busy(False)
        self.status_label.set_text("Tab 切换 · Enter 打开条目 · Esc 返回阅读 · 登录仅保留本次会话")
        if self.opds_client is None:
            self.opds_setup()
        elif self.opds_feed is None:
            self.opds_browse(self.opds_client.url, reset=True)

    def opds_setup(self):
        if self.opds_busy:
            self.opds_cancel_request()
        dialog = Gtk.Dialog(title="连接 Calibre / OPDS", transient_for=self.window, modal=True)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "连接", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_border_width(16)
        box.set_spacing(10)
        address = Gtk.Entry()
        address.set_width_chars(38)
        address.set_placeholder_text("http://服务器:8080/opds")
        address.set_max_length(4096)
        address.set_text(self.opds_client.url if self.opds_client else load_server(state_path().parent / "opds.json"))
        username, password = Gtk.Entry(), Gtk.Entry()
        username.set_max_length(200)
        password.set_max_length(500)
        password.set_visibility(False)
        for caption, entry in (("服务器地址", address), ("用户名（可留空）", username), ("密码（仅当前会话）", password)):
            box.pack_start(self.label(caption, "muted"), False, False, 0)
            entry.set_activates_default(True)
            box.pack_start(entry, False, False, 0)
        box.pack_start(self.label("支持 HTTP / HTTPS，自动响应 Basic / Digest 登录。\n仅记住标准 /opds 地址；自定义路径和登录信息不保存。", "muted", True), False, False, 0)
        dialog.show_all()
        response = dialog.run()
        values = address.get_text(), username.get_text(), password.get_text()
        password.set_text("")
        username.set_text("")
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        try:
            client = Client(*values)
            save_server(state_path().parent / "opds.json", client.url)
            self.opds_client = client
            self.opds_feed = None
            self.opds_history.clear()
            self.opds_browse(client.url, reset=True)
        except (OPDSError, OSError) as exc:
            self.message(str(exc) if isinstance(exc, OPDSError) else "无法保存书库设置，请检查可用空间")
        finally:
            values = None

    def opds_set_busy(self, busy):
        self.opds_busy = busy
        self.opds_cancel.set_sensitive(busy)
        self.opds_previous.set_sensitive(not busy and bool(self.opds_feed and self.opds_feed.previous))
        self.opds_next.set_sensitive(not busy and bool(self.opds_feed and self.opds_feed.next))
        self.opds_back.set_sensitive(not busy and bool(self.opds_history))
        self.opds_search_button.set_sensitive(not busy and bool(self.opds_feed and self.opds_feed.search))
        self.opds_list.set_sensitive(not busy)

    def opds_browse(self, url, reset=False, descend=False):
        if not url or not self.opds_client:
            return
        client = self.opds_client
        self.opds_start(lambda cancel: client.browse(url, cancel), reset, descend)

    def opds_start(self, work, reset=False, descend=False):
        previous = self.opds_feed
        self.opds_set_busy(True)
        self.status_label.set_text("正在读取书库…可取消；当前图书与阅读进度保留")
        def done(feed, error):
            self.opds_set_busy(False)
            if error:
                self.message(str(error) if isinstance(error, OPDSError) else "书库操作失败，请检查网络后重试")
                self.status_label.set_text("书库未更新；可刷新或重新登录")
                return
            if reset:
                self.opds_history.clear()
            elif descend and previous:
                self.opds_history = (self.opds_history + [previous])[-12:]
            self.opds_feed = feed
            self.opds_render()
        self.submit(work, done)

    def opds_render(self):
        for child in self.opds_list.get_children():
            child.destroy()
        for child in self.opds_breadcrumbs.get_children():
            child.destroy()
        for index in range(max(0, len(self.opds_history) - 3), len(self.opds_history)):
            feed = self.opds_history[index]
            self.opds_breadcrumbs.pack_start(self.button(feed.title[:10] + " ›", lambda at=index: self.opds_go_back(at)), False, False, 0)
        self.opds_breadcrumbs.pack_start(self.label(self.opds_feed.title, "accent"), True, True, 0)
        for entry in self.opds_feed.entries:
            row = Gtk.ListBoxRow()
            row.entry = entry
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            content.set_border_width(10)
            content.pack_start(self.label(entry.title, "book-title"), False, False, 0)
            if entry.author:
                content.pack_start(self.label(entry.author, "muted"), False, False, 0)
            description = " · ".join(link.extension[1:].upper() for link in entry.acquisitions) if entry.acquisitions else "进入分类 ›" if entry.navigation else "暂无支持的阅读格式"
            content.pack_start(self.label(description, "accent"), False, False, 0)
            row.add(content)
            self.opds_list.add(row)
        if not self.opds_feed.entries:
            row = Gtk.ListBoxRow()
            row.entry = None
            row.add(self.label("没有匹配的图书", "muted"))
            self.opds_list.add(row)
        self.opds_list.show_all()
        self.opds_breadcrumbs.show_all()
        self.opds_set_busy(False)
        self.status_label.set_text(f"{len(self.opds_feed.entries)} 项 · 下载支持 EPUB / TXT / Markdown / CBZ · Esc 返回阅读")
        row = self.opds_list.get_row_at_index(0)
        if row:
            self.opds_list.select_row(row)
            row.grab_focus()

    def opds_go_back(self, index=None):
        if not self.opds_history:
            return
        self.opds_cancel_request()
        at = len(self.opds_history) - 1 if index is None else index
        self.opds_feed = self.opds_history[at]
        self.opds_history = self.opds_history[:at]
        self.opds_render()

    def opds_refresh(self):
        self.opds_browse(self.opds_feed.url if self.opds_feed else self.opds_client.url if self.opds_client else None)

    def opds_search(self):
        if self.opds_busy or not self.opds_feed or not self.opds_feed.search:
            return
        client, link, query = self.opds_client, self.opds_feed.search, self.opds_query.get_text()
        self.opds_start(lambda cancel: client.search(link, query, cancel), descend=True)

    def opds_activate(self, entry):
        if self.opds_busy or not entry:
            return
        if not entry.acquisitions:
            if entry.navigation:
                self.opds_browse(entry.navigation.url, descend=True)
            else:
                self.message("此条目没有可直接下载的支持格式；不支持 PDF、AZW、MOBI、借阅和付费流程。")
            return
        dialog = Gtk.Dialog(title="下载并阅读", transient_for=self.window, modal=True)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "下载并打开", Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_border_width(16)
        content.set_spacing(10)
        title = self.label(entry.title, "section-title", True)
        title.set_max_width_chars(42)
        title.set_lines(2)
        content.pack_start(title, False, False, 0)
        if entry.summary:
            summary = self.label(entry.summary[:500], "muted", True)
            summary.set_max_width_chars(45)
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_min_content_height(80)
            scroll.set_max_content_height(150)
            scroll.add(summary)
            content.pack_start(scroll, True, True, 0)
        formats = Gtk.ComboBoxText()
        for index, link in enumerate(entry.acquisitions):
            formats.append(str(index), link.extension[1:].upper())
        formats.set_active(0)
        content.pack_start(formats, False, False, 0)
        dialog.show_all()
        result = dialog.run()
        selected = formats.get_active()
        dialog.destroy()
        if result == Gtk.ResponseType.OK:
            self.opds_download(entry, entry.acquisitions[selected])

    def opds_download(self, entry, link):
        client = self.opds_client
        self.opds_set_busy(True)
        self.status_label.set_text("正在下载图书…可取消，原有阅读进度保留")
        def work(cancel):
            path = client.download(link, state_path().parent / "downloads", entry.title, cancel)
            return load_document(path, cancel)
        def done(document, error):
            self.opds_set_busy(False)
            if error:
                self.message(str(error) if isinstance(error, OPDSError) else "下载的图书无法打开，原有图书与进度保留")
                self.status_label.set_text("未打开新图书；可重试或返回阅读")
                return
            self.document_loaded(document, None)
        self.submit(work, done)

    def opds_cancel_request(self):
        self._cancel.set()
        self.opds_set_busy(False)
        self.status_label.set_text("书库请求已取消，原有图书与进度保留")

    def opds_return(self):
        self.opds_cancel_request()
        if self.document:
            self.main_stack.set_visible_child_name("reading")
            self.title_label.set_text(self.document.title)
            self.subtitle_label.set_text("本地文件 / " + self.document.path.suffix[1:].upper())
            self.update_location()
        else:
            self.show_shelf()
