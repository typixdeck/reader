"""Bounded local document parsing; EPUB never executes markup or fetches URLs."""
from __future__ import annotations

import hashlib
import posixpath
import re
import stat
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_MARKUP_BYTES = 8 * 1024 * 1024
MAX_EPUB_TEXT_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 5000
MAX_CHAPTERS = 1000
MAX_TITLE_CHARS = 200
CHAPTER_CHARS = 12000
CancelCheck = Callable[[], bool] | None


class ReaderFormatError(ValueError):
    pass


class LoadCancelled(ReaderFormatError):
    pass


def _check(cancel: CancelCheck) -> None:
    if cancel and cancel():
        raise LoadCancelled("已取消打开")


@dataclass(frozen=True)
class Chapter:
    title: str
    text: str


@dataclass(frozen=True)
class Document:
    path: Path
    kind: str
    title: str
    chapters: tuple[Chapter, ...] = ()
    # CBZ pages are ZIP member names, never a whole comic resident in RAM.
    images: tuple[str, ...] = ()
    sha256: str = ""

    @property
    def length(self) -> int:
        return len(self.chapters) if self.kind == "text" else len(self.images)


def _regular_file(path: Path, limit: int) -> None:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ReaderFormatError("请选择普通图书文件")
    if info.st_size > limit:
        raise ReaderFormatError(f"文件过大：此格式最多支持 {limit // (1024 * 1024)} MB")


def file_hash(path: Path, cancel: CancelCheck = None) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            _check(cancel)
            block = stream.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_FILE_BYTES:
                raise ReaderFormatError("文件在读取时增大，已停止打开")
            digest.update(block)
    return digest.hexdigest()


def _split_chapter(title: str, text: str) -> list[Chapter]:
    title = title[:MAX_TITLE_CHARS]
    chunks: list[str] = []
    start = 0
    while len(text) - start > CHAPTER_CHARS:
        split = text.rfind("\n", start, start + CHAPTER_CHARS)
        if split < start + CHAPTER_CHARS // 2:
            split = start + CHAPTER_CHARS
        value = text[start:split].strip()
        if value:
            chunks.append(value)
            if len(chunks) > MAX_CHAPTERS:
                raise ReaderFormatError("正文包含过多章节，最多支持 1000 节")
        start = split
        while start < len(text) and text[start] == "\n":
            start += 1
    if text[start:].strip():
        chunks.append(text[start:].strip())
    if len(chunks) > MAX_CHAPTERS:
        raise ReaderFormatError("正文包含过多章节，最多支持 1000 节")
    return [Chapter(title if len(chunks) == 1 else f"{title} · {i + 1}", chunk)
            for i, chunk in enumerate(chunks)]


def load_text(path: Path, cancel: CancelCheck = None) -> Document:
    _regular_file(path, MAX_TEXT_BYTES)
    with path.open("rb") as stream:
        data = stream.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise ReaderFormatError("文本超过 16 MB")
    _check(cancel)
    text = None
    for encoding in ("utf-8-sig", "gb18030", "big5"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            pass
    if text is None or "\x00" in text:
        raise ReaderFormatError("无法识别文本编码，请转为 UTF-8 后重试")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    chapters: list[Chapter] = []
    if path.suffix.lower() in {".md", ".markdown"}:
        title, start = "正文", 0
        # Iterate heading matches without allocating a list for every line.
        for heading in re.finditer(r"(?m)^#{1,3}[ \t]+(\S[^\n]*)", text):
            _check(cancel)
            chapters.extend(_split_chapter(title, text[start:heading.start()]))
            if len(chapters) >= MAX_CHAPTERS:
                raise ReaderFormatError("Markdown 包含过多章节，最多支持 1000 节")
            title, start = heading.group(1).strip()[:MAX_TITLE_CHARS], heading.start()
        _check(cancel)
        chapters.extend(_split_chapter(title, text[start:]))
    else:
        chapters = _split_chapter("正文", text)
    if not chapters:
        raise ReaderFormatError("文件为空，没有可读正文")
    if len(chapters) > MAX_CHAPTERS:
        raise ReaderFormatError("正文包含过多章节，最多支持 1000 节")
    return Document(path, "text", path.stem, tuple(chapters), sha256=file_hash(path, cancel))


def _archive_check(archive: zipfile.ZipFile) -> None:
    members = archive.infolist()
    if len(members) > MAX_MEMBERS or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
        raise ReaderFormatError("压缩包展开后过大，或包含过多文件")
    if len({item.filename for item in members}) != len(members):
        raise ReaderFormatError("压缩包含有重复文件名")
    for item in members:
        if item.flag_bits & 1:
            raise ReaderFormatError("暂不支持加密图书，请先提供未加密副本")
        if item.file_size > MAX_MEMBER_BYTES:
            raise ReaderFormatError("压缩包中的单个文件超过 32 MB")


def _member(archive: zipfile.ZipFile, name: str, limit: int, cancel: CancelCheck = None) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise ReaderFormatError("图书章节或图片过大")
    parts: list[bytes] = []
    total = 0
    with archive.open(info) as stream:
        while True:
            _check(cancel)
            block = stream.read(min(262144, limit - total + 1))
            if not block:
                break
            total += len(block)
            if total > limit:
                raise ReaderFormatError("图书内容超出读取上限")
            parts.append(block)
    return b"".join(parts)


def _xml(data: bytes) -> ET.Element:
    # EPUB containers and OPF need no entities; reject DTD before parsing.
    normalized = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in normalized or b"<!ENTITY" in normalized:
        raise ReaderFormatError("图书元数据包含不支持的 XML 实体")
    return ET.fromstring(data)


def _local_member(base: str, href: str) -> str:
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        raise ReaderFormatError("图书引用了远程章节，仅支持本地内容")
    value = unquote(parts.path)
    result = posixpath.normpath(posixpath.join(base, value))
    if result.startswith(("/", "../")) or result == ".." or "\\" in result or "\x00" in result:
        raise ReaderFormatError("图书包含无效章节路径")
    return result


class _ReadableHTML(HTMLParser):
    BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "section", "article", "blockquote", "tr", "br"}
    HIDDEN = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self.hidden = 0
        self.in_head = False
        self.in_title = False
        self.in_body = False
        self.saw_body = False

    def handle_starttag(self, tag: str, _attrs: list) -> None:
        if tag in self.HIDDEN:
            self.hidden += 1
        if tag == "head":
            self.in_head = True
        if tag == "title":
            self.in_title = True
        if tag == "body":
            self.in_body = self.saw_body = True
            self.parts.clear()
        if not self.hidden and not self.in_head and tag in self.BLOCKS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False
        if tag == "head":
            self.in_head = False
        if tag == "body":
            self.in_body = False
        if not self.hidden and not self.in_head and tag in self.BLOCKS:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)
        elif not self.hidden and not self.in_head and (self.in_body or not self.saw_body):
            self.parts.append(data)


def _xhtml_text(data: bytes) -> tuple[str, str]:
    parser = _ReadableHTML()
    parser.feed(data.decode("utf-8-sig", errors="replace"))
    parser.close()
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n(?:\s*\n)*", "\n\n", text)
    return "".join(parser.title).strip(), text.strip()


def load_epub(path: Path, cancel: CancelCheck = None) -> Document:
    try:
        with zipfile.ZipFile(path) as archive:
            _archive_check(archive)
            container = _xml(_member(archive, "META-INF/container.xml", MAX_MARKUP_BYTES, cancel))
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None or not rootfile.get("full-path"):
                raise ReaderFormatError("EPUB 缺少 OPF")
            opf_path = _local_member("", rootfile.get("full-path", ""))
            opf = _xml(_member(archive, opf_path, MAX_MARKUP_BYTES, cancel))
            manifest: dict[str, tuple[str, str]] = {}
            for item in opf.findall(".//{*}manifest/{*}item"):
                item_id, href = item.get("id"), item.get("href")
                if item_id and href:
                    # Resolve only spine entries; unrelated remote assets are ignored.
                    manifest[item_id] = (href, item.get("media-type", ""))
            title = next((node.text or "" for node in opf.findall(".//{*}metadata/{*}title")), path.stem)
            chapters: list[Chapter] = []
            text_bytes = 0
            for itemref in opf.findall(".//{*}spine/{*}itemref"):
                _check(cancel)
                entry = manifest.get(itemref.get("idref", ""))
                if entry is None:
                    raise ReaderFormatError("EPUB 的章节清单不完整")
                href, media_type = entry
                if media_type not in {"application/xhtml+xml", "text/html"}:
                    continue
                source = _local_member(posixpath.dirname(opf_path), href)
                data = _member(archive, source, MAX_MARKUP_BYTES, cancel)
                text_bytes += len(data)
                if text_bytes > MAX_EPUB_TEXT_BYTES:
                    raise ReaderFormatError("EPUB 正文超出读取上限")
                chapter_title, body = _xhtml_text(data)
                chapters.extend(_split_chapter(chapter_title or f"第 {len(chapters) + 1} 节", body))
                if len(chapters) > MAX_CHAPTERS:
                    raise ReaderFormatError("EPUB 正文包含过多章节")
            if not chapters:
                raise ReaderFormatError("EPUB 没有可读文字章节")
            return Document(path, "text", title.strip()[:MAX_TITLE_CHARS] or path.stem, tuple(chapters), sha256=file_hash(path, cancel))
    except (KeyError, zipfile.BadZipFile, ET.ParseError, RuntimeError, NotImplementedError) as exc:
        raise ReaderFormatError("EPUB 损坏、加密或使用不支持的压缩格式") from exc


def _natural_key(name: str) -> list[tuple[int, str | int]]:
    return [(1, int(part)) if part.isdigit() else (0, part.casefold()) for part in re.split(r"(\d+)", name)]


def load_cbz(path: Path, cancel: CancelCheck = None) -> Document:
    try:
        with zipfile.ZipFile(path) as archive:
            _archive_check(archive)
            names = sorted((item.filename for item in archive.infolist()
                            if not item.is_dir() and item.file_size > 0
                            and not item.filename.startswith(("/", "..", "__MACOSX/"))
                            and Path(item.filename).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}),
                           key=_natural_key)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ReaderFormatError("CBZ 损坏或使用不支持的压缩格式") from exc
    if not names:
        raise ReaderFormatError("CBZ 中没有可读图片")
    if len(names) > MAX_CHAPTERS:
        raise ReaderFormatError("漫画包含过多图片页，最多支持 1000 页")
    return Document(path, "image", path.stem, images=tuple(names), sha256=file_hash(path, cancel))


def read_image(document: Document, index: int, cancel: CancelCheck = None) -> bytes:
    try:
        with zipfile.ZipFile(document.path) as archive:
            return _member(archive, document.images[index], MAX_MEMBER_BYTES, cancel)
    except (KeyError, IndexError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ReaderFormatError("图片页无法读取，文件可能已被移动或损坏") from exc


def load_document(path: Path, cancel: CancelCheck = None) -> Document:
    path = path.expanduser().resolve()
    _check(cancel)
    suffix = path.suffix.lower()
    parsers = {".txt": load_text, ".md": load_text, ".markdown": load_text,
               ".epub": load_epub, ".cbz": load_cbz}
    if suffix not in parsers:
        raise ReaderFormatError(f"暂不支持 {suffix or '无扩展名文件'}；可打开 TXT、Markdown、EPUB 和 CBZ")
    _regular_file(path, MAX_FILE_BYTES)
    return parsers[suffix](path, cancel)
