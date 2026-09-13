"""Bounded OPDS 1 Atom client for Calibre. Credentials never leave memory."""
from __future__ import annotations

import hashlib
import html
import http.client
import io
import json
import os
import re
import shutil
import socket
import queue
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .formats import LoadCancelled

ATOM = "{http://www.w3.org/2005/Atom}"
XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
MAX_FEED = 2 * 1024 * 1024
MAX_DOWNLOAD = 128 * 1024 * 1024
MAX_ENTRIES = 250
# Calibre's published MIME registry names MOBI7/PRC and KF8 separately.
# https://github.com/kovidgoyal/calibre/blob/master/resources/mime.types
FORMATS = {
    "application/epub+zip": ".epub",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/x-markdown": ".md",
    "application/x-cbz": ".cbz",
    "application/vnd.comicbook+zip": ".cbz",
    "application/pdf": ".pdf",
    "application/x-mobipocket-ebook": ".mobi",
    "application/x-mobi8-ebook": ".azw3",
    "application/vnd.amazon.ebook": ".azw",
}
_OPEN_SLOTS = threading.BoundedSemaphore(2)

ACQUIRE = {"http://opds-spec.org/acquisition", "http://opds-spec.org/acquisition/open-access"}


class OPDSError(ValueError):
    pass


def checked_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096 or any(ord(char) < 32 for char in url):
        raise OPDSError("书库地址无效")
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None or parts.fragment:
            raise ValueError()
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError()
    except ValueError as exc:
        raise OPDSError("仅支持不含账号、密码或片段的 HTTP/HTTPS 地址") from exc
    return url


def server_url(url: str) -> str:
    url = checked_url(url.strip())
    parts = urllib.parse.urlsplit(url)
    if parts.query:
        raise OPDSError("服务器地址不能包含查询令牌；账号密码请填在登录字段")
    path = "/opds" if parts.path in {"", "/"} else parts.path
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def origin(url):
    parts = urllib.parse.urlsplit(checked_url(url))
    return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)


def same_origin(url, base):
    checked_url(url)
    if origin(url) != origin(base):
        raise OPDSError("书库链接跳转到其他服务器，已阻止发送请求和登录信息")
    return url


def resolve_url(base, href):
    return same_origin(urllib.parse.urljoin(base, href), base)


def xml_root(data):
    if len(data) > MAX_FEED:
        raise OPDSError("书库目录超过 2 MiB 上限，请使用分页目录")
    if re.search(br"<!\s*(DOCTYPE|ENTITY)\b", data.replace(b"\x00", b""), re.I):
        raise OPDSError("书库 XML 不允许 DTD 或实体声明")
    try:
        depth = count = 0
        parser = ET.iterparse(io.BytesIO(data), events=("start", "end"))
        for event, _element in parser:
            if event == "start":
                depth += 1
                count += 1
                if depth > 32 or count > 10000:
                    raise OPDSError("书库目录结构过大或过深")
            else:
                depth -= 1
        return parser.root
    except ET.ParseError as exc:
        raise OPDSError("返回内容不是有效 OPDS XML；请确认服务器地址以 /opds 开头") from exc


def text(element, maximum=300):
    if element is None:
        return ""
    return html.unescape(re.sub(r"<[^>]*>", "", "".join(element.itertext())))[:maximum].strip()


@dataclass(frozen=True)
class Link:
    url: str
    relation: str
    mime: str
    title: str = ""

    @property
    def extension(self):
        return FORMATS.get(self.mime.split(";", 1)[0].lower())


@dataclass(frozen=True)
class Entry:
    title: str
    author: str
    summary: str
    navigation: Link | None
    acquisitions: tuple[Link, ...]
    unsupported: bool = False


@dataclass(frozen=True)
class Feed:
    url: str
    title: str
    entries: tuple[Entry, ...]
    previous: str | None = None
    next: str | None = None
    search: Link | None = None


def links(element, base):
    current = resolve_url(base, element.get(XML_BASE, ""))
    result = []
    for node in element.findall(ATOM + "link")[:40]:
        href = node.get("href", "")
        if not href:
            continue
        # Query templates are resolved without interpolating any credentials.
        try:
            url = resolve_url(resolve_url(current, node.get(XML_BASE, "")), href)
        except OPDSError:
            continue
        result.append(Link(url, node.get("rel", "alternate"), node.get("type", ""), node.get("title", "")[:200]))
    return result


def parse_feed(data, url):
    root = xml_root(data)
    if root.tag not in {ATOM + "feed", ATOM + "entry"}:
        raise OPDSError("仅支持 OPDS 1 Atom 目录；服务器返回了其他格式")
    base = resolve_url(url, root.get(XML_BASE, ""))
    nodes = [root] if root.tag == ATOM + "entry" else root.findall(ATOM + "entry")
    if len(nodes) > MAX_ENTRIES:
        raise OPDSError("目录超过 250 项，请在服务器启用分页")
    entries = []
    for node in nodes:
        available = links(node, url if root.tag == ATOM + "entry" else base)
        acquire = tuple(item for item in available if item.relation in ACQUIRE and item.extension)
        navigate = next((item for item in available if item.mime.startswith("application/atom+xml") and item.relation in {"alternate", "subsection", "http://opds-spec.org/sort/new", "http://opds-spec.org/sort/popular"}), None)
        unsupported = any(item.relation.startswith("http://opds-spec.org/acquisition") for item in available) and not acquire
        entries.append(Entry(text(node.find(ATOM + "title")) or "未命名条目", "、".join(text(author.find(ATOM + "name")) for author in node.findall(ATOM + "author")[:4]), text(node.find(ATOM + "summary"), 1600) or text(node.find(ATOM + "content"), 1600), navigate, acquire, unsupported))
    available = links(root, url)
    by_rel = {item.relation: item for item in available}
    return Feed(url, text(root.find(ATOM + "title")) or "Calibre 书库", tuple(entries), by_rel["previous"].url if "previous" in by_rel else None, by_rel["next"].url if "next" in by_rel else None, by_rel.get("search"))


def expand_search(template, query):
    if not query.strip() or len(query) > 200:
        raise OPDSError("请输入 1–200 字符的搜索内容")
    values = {"searchTerms": urllib.parse.quote(query.strip(), safe=""), "count": "50", "startIndex": "1", "startPage": "1", "language": "*", "inputEncoding": "UTF-8", "outputEncoding": "UTF-8"}
    def replace(match):
        token = match.group(1)
        optional = token.endswith("?")
        token = token.rstrip("?")
        if token in values:
            return values[token]
        if optional:
            return ""
        raise OPDSError("服务器搜索模板包含不支持的必填参数")
    if "{searchTerms}" not in template and "{searchTerms?}" not in template:
        # Calibre can expose a direct query link with an empty query parameter.
        parts = urllib.parse.urlsplit(template)
        params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        for index, (key, _value) in enumerate(params):
            if key in {"query", "search", "q"}:
                params[index] = key, query.strip()
                return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(params)))
        raise OPDSError("服务器没有提供可用的搜索模板")
    return re.sub(r"\{([^{}]+)\}", replace, template)


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 4
    max_repeats = 2
    def __init__(self, base):
        self.base = base
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            same_origin(newurl, self.base)
        except OPDSError:
            fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ClosingBasicAuth(urllib.request.HTTPBasicAuthHandler):
    def http_error_401(self, req, fp, code, msg, headers):
        try:
            return super().http_error_401(req, fp, code, msg, headers)
        finally:
            fp.close()


class ClosingDigestAuth(urllib.request.HTTPDigestAuthHandler):
    def http_error_401(self, req, fp, code, msg, headers):
        try:
            return super().http_error_401(req, fp, code, msg, headers)
        finally:
            fp.close()


class TrackedHTTP(urllib.request.HTTPHandler):
    def __init__(self, connections):
        super().__init__()
        self.connections = connections
    def http_open(self, request):
        def connection(host, **kwargs):
            value = http.client.HTTPConnection(host, **kwargs)
            self.connections.append(value)
            return value
        return self.do_open(connection, request)


class TrackedHTTPS(urllib.request.HTTPSHandler):
    def __init__(self, connections):
        super().__init__()
        self.connections = connections
    def https_open(self, request):
        def connection(host, **kwargs):
            value = http.client.HTTPSConnection(host, **kwargs)
            self.connections.append(value)
            return value
        options = {"context": self._context}
        # Python 3.12 removed HTTPSConnection.check_hostname; 3.13 removed
        # the corresponding urllib handler attribute. Keep the system TLS
        # context and hostname verification on both supported Pi OS versions.
        if hasattr(self, "_check_hostname"):
            options["check_hostname"] = self._check_hostname
        return self.do_open(connection, request, **options)


class Client:
    def __init__(self, url, username="", password="", opener=None):
        self.url = server_url(url)
        self._next_request = 0.0
        self._connections = []
        self._opening = False
        credentials = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        # Auth only responds to same-origin challenges; redirects are checked
        # before issuing a second request. No Authorization in a URL or state.
        parts = urllib.parse.urlsplit(self.url)
        credentials.add_password(None, urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/", "", "")), username, password)
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), TrackedHTTP(self._connections), TrackedHTTPS(self._connections), SameOriginRedirect(self.url), ClosingDigestAuth(credentials), ClosingBasicAuth(credentials))

    def _check(self, cancel, deadline):
        if cancel():
            raise LoadCancelled("书库操作已取消")
        if time.monotonic() > deadline:
            raise OPDSError("书库响应超时，请稍后重试")

    def _open(self, url, cancel):
        same_origin(url, self.url)
        if time.monotonic() < self._next_request:
            raise OPDSError("服务器请求过于频繁，请稍等后重试")
        self._check(cancel, time.monotonic() + 1)
        request = urllib.request.Request(url, headers={"Accept": "application/atom+xml,application/opensearchdescription+xml,*/*;q=0.5", "Accept-Encoding": "identity", "User-Agent": "TypixReader/0.3"})
        try:
            response = self._open_bounded(request, cancel)
            try:
                same_origin(response.geturl(), self.url)
            except OPDSError:
                response.close()
                raise
            return response
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in {429, 503}:
                delay = exc.headers.get("Retry-After", "2")
                self._next_request = time.monotonic() + min(60, int(delay) if delay.isdigit() else 2)
            if exc.code in {401, 403}:
                raise OPDSError("书库登录失败或没有权限，请检查账号和密码") from None
            raise OPDSError(f"书库请求失败（HTTP {exc.code}），请稍后重试") from None
        except (OSError, urllib.error.URLError, ValueError, http.client.HTTPException):
            if cancel():
                raise LoadCancelled("书库操作已取消")
            raise OPDSError("无法连接书库，请检查网络、地址或 HTTPS 证书后重试") from None

    def _open_bounded(self, request, cancel):
        # urllib reads HTTP headers/auth challenges before returning a response.
        # Bound that phase too; close its tracked socket on cancellation/deadline.
        # At most two DNS/header workers can exist globally, including abandoned
        # OS DNS calls that cannot be interrupted by Python's socket timeout.
        if self._opening or not _OPEN_SLOTS.acquire(blocking=False):
            raise OPDSError("上次连接仍在结束，请稍后重试")
        self._opening = True
        self._connections.clear()
        result = queue.Queue(maxsize=1)
        abandoned = threading.Event()
        def open_request():
            response = None
            try:
                response = self.opener.open(request, timeout=10)
                if abandoned.is_set():
                    response.close()
                else:
                    result.put((response, None))
            except Exception as exc:
                if not abandoned.is_set():
                    result.put((None, exc))
            finally:
                self._opening = False
                _OPEN_SLOTS.release()
        threading.Thread(target=open_request, name="opds-headers", daemon=True).start()
        deadline = time.monotonic() + 30
        try:
            while True:
                self._check(cancel, deadline)
                try:
                    response, error = result.get(timeout=0.05)
                except queue.Empty:
                    continue
                if error:
                    raise error
                return response
        except (LoadCancelled, OPDSError):
            abandoned.set()
            for connection in tuple(self._connections):
                sock = connection.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                connection.close()
            try:
                response, _error = result.get_nowait()
                if response:
                    response.close()
            except queue.Empty:
                pass
            raise

    def fetch(self, url, cancel=lambda: False):
        deadline = time.monotonic() + 40
        try:
            with self._open(url, cancel) as response:
                contents = bytearray()
                while True:
                    self._check(cancel, deadline)
                    block = response.read1(min(65536, MAX_FEED + 1 - len(contents)))
                    if not block:
                        break
                    contents.extend(block)
                    if len(contents) > MAX_FEED:
                        raise OPDSError("书库目录过大，请启用分页")
                return bytes(contents), response.geturl()
        except (OSError, urllib.error.URLError):
            raise OPDSError("书库连接中断，请重试；本地图书未改变") from None

    def browse(self, url=None, cancel=lambda: False):
        data, final = self.fetch(url or self.url, cancel)
        return parse_feed(data, final)

    def search(self, link, query, cancel=lambda: False):
        template = link.url
        if link.mime.startswith("application/opensearchdescription+xml"):
            data, final = self.fetch(link.url, cancel)
            root = xml_root(data)
            options = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "Url" and node.get("type", "").startswith("application/atom+xml")]
            if not options:
                raise OPDSError("OpenSearch 未提供 OPDS Atom 搜索")
            template = resolve_url(final, options[0].get("template", ""))
        return self.browse(same_origin(expand_search(template, query), self.url), cancel)

    def download(self, link, directory: Path, title, cancel=lambda: False, progress=lambda *_: None):
        if not link.extension or link.relation not in ACQUIRE:
            raise OPDSError("此条目只提供不支持、借阅或付费格式；支持 EPUB、TXT、Markdown、CBZ")
        directory.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(directory).free < MAX_DOWNLOAD + 16 * 1024 * 1024:
            raise OPDSError("下载空间不足，请至少保留 144 MiB 可用空间")
        deadline = time.monotonic() + 300
        temporary = None
        try:
            with self._open(link.url, cancel) as response:
                length = response.headers.get("Content-Length")
                expected = int(length) if length and length.isdigit() else None
                if expected is not None and (expected <= 0 or expected > MAX_DOWNLOAD):
                    raise OPDSError("图书为空或超过 128 MiB 下载上限")
                descriptor, temporary = tempfile.mkstemp(prefix=".opds-", dir=directory)
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(descriptor, "wb") as output:
                    while True:
                        self._check(cancel, deadline)
                        block = response.read1(min(262144, MAX_DOWNLOAD - size + 1))
                        if not block:
                            break
                        size += len(block)
                        if size > MAX_DOWNLOAD:
                            raise OPDSError("图书超过 128 MiB 下载上限")
                        output.write(block)
                        digest.update(block)
                        progress(size, expected)
                    if not size or (expected is not None and size != expected):
                        raise OPDSError("图书下载不完整，请重试")
                    output.flush()
                    os.fsync(output.fileno())
                self._check(cancel, deadline)
                safe_title = re.sub(r"[^\w -]+", "_", title, flags=re.UNICODE).strip(" ._")[:60] or "book"
                target = directory / (safe_title + link.extension)
                # Atomic no-replace publication preserves locally edited books.
                # Never overwrite an existing pathname, including symlinks.
                for attempt in range(100):
                    candidate = target if not attempt else target.with_stem(target.stem + f"-{attempt}")
                    try:
                        os.link(temporary, candidate)
                        return candidate
                    except FileExistsError:
                        continue
                raise OPDSError("同名下载过多，请整理本地图书后重试")
        except (OSError, urllib.error.URLError):
            raise OPDSError("图书下载中断或空间不足，请重试；原有图书未改变") from None
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)


def load_server(path):
    try:
        if path.stat().st_size > 8192:
            return ""
        value = server_url(json.loads(path.read_text())["server"])
        return value if urllib.parse.urlsplit(value).path in {"/opds", "/opds/"} else ""
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def save_server(path, url):
    value = server_url(url)
    if urllib.parse.urlsplit(value).path not in {"/opds", "/opds/"}:
        # Arbitrary URL paths may contain opaque authentication tokens.
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".opds-config-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump({"server": value}, output)
        os.replace(temporary, path)
        return True
    finally:
        Path(temporary).unlink(missing_ok=True)
