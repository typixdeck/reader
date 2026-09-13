"""Bounded, cancellable PDF requests; native parsing stays outside GTK."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time

from .formats import Document, LoadCancelled, ReaderFormatError, _check, file_hash

MAX_PDF_BYTES = 128 * 1024 * 1024
MAX_PDF_PAGES = 1000
MAX_PNG_BYTES = 32 * 1024 * 1024
_active = set()
_active_lock = threading.Lock()
ERRORS = {
    "dependency": "PDF 组件未安装，请安装 gir1.2-poppler-0.18 和 python3-gi-cairo 后重试",
    "encrypted": "PDF 已加密或需要密码；请提供拥有权限的未加密副本",
    "pages": "PDF 为空或超过 1000 页上限",
    "geometry": "PDF 页面尺寸无效或超过渲染上限",
    "changed": "PDF 已被修改或替换，请重新打开",
    "invalid": "PDF 损坏或使用不支持的内容，原有图书保留",
    "resource": "PDF 超出处理资源或临时存储上限，请尝试较小文件",
}


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=0.4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def stop_pdf_workers():
    """Reap native workers before the GUI's daemon loader can be torn down."""
    with _active_lock:
        for process in tuple(_active):
            _stop(process)


def request(path, action, cancel=None, expected=(), timeout=20, **options):
    _check(cancel)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_PDF_BYTES:
            raise ReaderFormatError("PDF 必须是普通文件，且不超过 128 MiB")
        if expected and fingerprint(info) != tuple(expected):
            raise ReaderFormatError(ERRORS["changed"])
        with tempfile.TemporaryDirectory(prefix="typix-pdf-") as directory:
            root = Path(directory)
            spec = {"action": action, "fd": descriptor, **options}
            (root / "request.json").write_text(json.dumps(spec))
            try:
                with _active_lock:
                    _check(cancel)
                    process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("pdf_worker.py")), str(root)],
                                               pass_fds=(descriptor,), stdin=subprocess.DEVNULL,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                               start_new_session=True)
                    _active.add(process)
            except OSError as exc:
                raise ReaderFormatError("无法启动 PDF 处理程序，请检查系统可用资源") from exc
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    _check(cancel)
                    if time.monotonic() > deadline:
                        raise ReaderFormatError("PDF 处理超时，可取消后打开其他图书")
                    time.sleep(0.04)
                _check(cancel)
                if fingerprint(os.fstat(descriptor)) != fingerprint(info):
                    raise ReaderFormatError(ERRORS["changed"])
                output = root / "result.json"
                if process.returncode or not output.is_file() or output.stat().st_size > 65536:
                    raise ReaderFormatError(ERRORS["resource"])
                result = json.loads(output.read_text())
                if "error" in result:
                    raise ReaderFormatError(ERRORS.get(result["error"], ERRORS["invalid"]))
                if action == "render":
                    png = root / "page.png"
                    if not png.is_file() or not 0 < png.stat().st_size <= MAX_PNG_BYTES:
                        raise ReaderFormatError(ERRORS["resource"])
                    result["png"] = png.read_bytes()
                result["fingerprint"] = fingerprint(info)
                return result
            finally:
                _stop(process)
                with _active_lock:
                    _active.discard(process)
    except OSError as exc:
        raise ReaderFormatError("PDF 无法读取或临时空间不足；原有图书保留") from exc
    finally:
        os.close(descriptor)


def load_pdf(path, cancel=None):
    metadata = request(path, "metadata", cancel)
    digest = file_hash(path, cancel)
    if fingerprint(path.stat()) != metadata["fingerprint"]:
        raise ReaderFormatError(ERRORS["changed"])
    return Document(path, "pdf", metadata.get("title") or path.stem,
                    sha256=digest, pages=metadata["pages"], source_stamp=metadata["fingerprint"])


def render_pdf(document, page, width, height, zoom=1.0, match=None, cancel=None):
    return request(document.path, "render", cancel, document.source_stamp,
                   page=page, width=width, height=height, zoom=zoom, match=match)


def search_pdf(document, query, page=0, after=-1, cancel=None):
    if not query.strip() or len(query) > 128:
        raise ReaderFormatError("请输入 1–128 字符的 PDF 搜索内容")
    return request(document.path, "search", cancel, document.source_stamp, timeout=30,
                   query=query, page=page, after=after).get("match")
