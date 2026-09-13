"""Local MOBI/KF8 text import through the distro's libmobi-tools.

Only the fixed ``mobitool -e`` operation is used. No DRM key, shell, GUI,
network request or user-book inventory is involved. Generated EPUB content
is read by our own text parser and removed before returning.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .formats import CancelCheck, Document, LoadCancelled, ReaderFormatError, load_epub

MOBITOOL = "/usr/bin/mobitool"
MAX_INPUT = 256 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
MAX_TEXT = 32 * 1024 * 1024
MAX_RECORDS = 32768
MAX_HEADER = 1024 * 1024
DISK_MARGIN = 32 * 1024 * 1024
TIMEOUT = 45.0
POLL = 0.05
DRM_ERROR = "此 Kindle 图书受 DRM 加密保护；Reader 只支持合法取得的未加密副本，不提供解密"
INVALID_ERROR = "MOBI/KF8 文件结构损坏或不受支持"
_workers_lock = threading.Lock()
_workers: set[subprocess.Popen] = set()
_shutting_down = threading.Event()

# A fresh, isolated interpreter sets limits before exec. preexec_fn is unsafe
# when called by the GTK loader thread. The converter writes one fixed EPUB.
_LIMIT_EXEC = """
import os, resource, sys
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
resource.setrlimit(resource.RLIMIT_FSIZE, (int(sys.argv[2]), int(sys.argv[2])))
resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
if sys.platform.startswith('linux'):
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
os.umask(0o077)
os.execv(sys.argv[1], [sys.argv[1], '-e', '-o', '.', 'source.mobi'])
"""


def _check(cancel: CancelCheck, deadline: float) -> None:
    if _shutting_down.is_set() or (cancel and cancel()):
        raise LoadCancelled("已取消打开")
    if time.monotonic() >= deadline:
        raise ReaderFormatError("Kindle 图书处理超时，请使用较小的未加密图书")


def _disk(directory: Path, required: int) -> None:
    if shutil.disk_usage(directory).free < required + DISK_MARGIN:
        raise ReaderFormatError("临时存储空间不足，无法打开 Kindle 图书；请释放空间后重试")


def _snapshot(path: Path, destination: Path, cancel: CancelCheck, deadline: float) -> str:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ReaderFormatError("请选择普通图书文件")
        if not 78 <= info.st_size <= MAX_INPUT:
            raise ReaderFormatError("Kindle 文件为空、损坏或超过 256 MB")
        _disk(destination.parent, info.st_size + MAX_OUTPUT)
        digest = hashlib.sha256()
        total = 0
        with destination.open("xb") as output:
            while True:
                _check(cancel, deadline)
                data = source.read(min(262144, MAX_INPUT - total + 1))
                if not data:
                    break
                total += len(data)
                if total > MAX_INPUT:
                    raise ReaderFormatError("图书在读取时增大，已停止打开")
                output.write(data)
                digest.update(data)
            output.flush()
        after = os.fstat(source.fileno())
        if total != info.st_size or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                info.st_size, info.st_mtime_ns, info.st_ctime_ns):
            raise ReaderFormatError("图书在读取时发生变化，请保存文件后重试")
        return digest.hexdigest()


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def _preflight(path: Path, cancel: CancelCheck, deadline: float) -> None:
    """Check PalmDB framing and BOTH hybrid headers before libmobi can decrypt.

    Offsets follow libmobi 0.12 src/mobi.h and read.c. In particular libmobi
    automatically attempts type-1 DRM, so checking only converter errors is
    insufficient. EXTH 121 names the KF8 header after a BOUNDARY record.
    """
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        head = stream.read(78)
        if len(head) != 78 or head[60:68] != b"BOOKMOBI":
            raise ReaderFormatError("只支持 MOBI/PRC 和 KF8/AZW/AZW3；不支持 KFX、Topaz 或其他 Kindle 容器")
        count = struct.unpack_from(">H", head, 76)[0]
        if not 2 <= count <= MAX_RECORDS or 78 + count * 8 > size:
            raise ReaderFormatError(INVALID_ERROR)
        table = stream.read(count * 8)
        offsets = [_u32(table, i * 8) for i in range(count)] + [size]
        if offsets[0] < 78 + count * 8 or any(a >= b for a, b in zip(offsets, offsets[1:])):
            raise ReaderFormatError(INVALID_ERROR)

        def record(index: int) -> bytes:
            if not 0 <= index < count or offsets[index + 1] - offsets[index] > MAX_HEADER:
                raise ReaderFormatError(INVALID_ERROR)
            stream.seek(offsets[index])
            return stream.read(offsets[index + 1] - offsets[index])

        def header(index: int) -> int | None:
            _check(cancel, deadline)
            data = record(index)
            if len(data) < 40 or data[16:20] != b"MOBI":
                raise ReaderFormatError(INVALID_ERROR)
            compression, = struct.unpack_from(">H", data)
            encryption, = struct.unpack_from(">H", data, 12)
            if encryption:
                raise ReaderFormatError(DRM_ERROR)
            text_count, record_size = struct.unpack_from(">HH", data, 8)
            if compression not in {1, 2, 17480} or not 0 < text_count < count - index:
                raise ReaderFormatError(INVALID_ERROR)
            if _u32(data, 4) > MAX_TEXT or text_count * record_size > 2 * MAX_TEXT:
                raise ReaderFormatError("Kindle 解压正文超过 32 MB，无法打开")
            length = _u32(data, 20)
            end = 16 + length
            if length < 24 or end > len(data) or _u32(data, 36) > 8:
                raise ReaderFormatError(INVALID_ERROR)
            if length >= 164 and (_u32(data, 172) or _u32(data, 176)):
                raise ReaderFormatError(DRM_ERROR)
            if length < 116 or not (_u32(data, 128) & 0x40):
                return None
            if data[end:end + 4] != b"EXTH" or end + 12 > len(data):
                raise ReaderFormatError(INVALID_ERROR)
            exth_end = end + _u32(data, end + 4)
            exth_count = _u32(data, end + 8)
            if not end + 12 <= exth_end <= len(data) or exth_count > 1024:
                raise ReaderFormatError(INVALID_ERROR)
            cursor, boundary = end + 12, None
            for _ in range(exth_count):
                if cursor + 8 > exth_end:
                    raise ReaderFormatError(INVALID_ERROR)
                tag, item_size = struct.unpack_from(">II", data, cursor)
                if item_size < 8 or cursor + item_size > exth_end:
                    raise ReaderFormatError(INVALID_ERROR)
                if tag == 121:
                    if item_size != 12 or boundary is not None:
                        raise ReaderFormatError(INVALID_ERROR)
                    boundary = _u32(data, cursor + 8)
                cursor += item_size
            return boundary

        boundary = header(0)
        if boundary not in (None, 0xFFFFFFFF):
            if not 2 <= boundary < count or not record(boundary - 1).startswith(b"BOUNDARY"):
                raise ReaderFormatError(INVALID_ERROR)
            if header(boundary) not in (None, 0xFFFFFFFF):
                raise ReaderFormatError(INVALID_ERROR)


def _stop(process: subprocess.Popen) -> None:
    # Only our short-lived converter process group; never an installed app.
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def cancel_active_workers() -> None:
    """Called by GTK shutdown before daemon loader threads can be abandoned."""
    with _workers_lock:
        _shutting_down.set()
        processes = tuple(_workers)
    for process in processes:
        _stop(process)


def _convert(directory: Path, cancel: CancelCheck, deadline: float) -> Path:
    if not os.path.isfile(MOBITOOL) or not os.access(MOBITOOL, os.X_OK):
        raise ReaderFormatError("缺少 Kindle 格式组件，请通过系统软件包管理器安装 libmobi-tools 后重试")
    output = directory / "source.epub"
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(directory), "TMPDIR": str(directory), "LC_ALL": "C"}
    # Serialize spawn/register against shutdown so no child can slip between
    # the shutdown snapshot and GTK's final process exit.
    with _workers_lock:
        _check(cancel, deadline)
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", _LIMIT_EXEC, MOBITOOL, str(MAX_OUTPUT)],
            cwd=directory, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        _workers.add(process)
    try:
        while process.poll() is None:
            _check(cancel, deadline)
            _disk(directory, 0)
            time.sleep(POLL)
        _check(cancel, deadline)
        if process.returncode != 0:
            raise ReaderFormatError("Kindle 转换失败：文件损坏、正文过大或格式不受支持")
        if not output.exists():
            raise ReaderFormatError("此 Kindle 图书没有可重建正文；暂不支持 Print Replica/AZW4")
        info = output.lstat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_OUTPUT:
            raise ReaderFormatError("Kindle 转换结果无效或过大")
        return output
    finally:
        _stop(process)
        with _workers_lock:
            _workers.discard(process)


def load_kindle(path: Path, cancel: CancelCheck = None) -> Document:
    """Return text chapters, preserving the original file's path and SHA-256."""
    path = Path(path)
    deadline = time.monotonic() + TIMEOUT
    _check(cancel, deadline)
    try:
        with tempfile.TemporaryDirectory(prefix="typix-reader-kindle-") as folder:
            directory = Path(folder)
            source = directory / "source.mobi"
            digest = _snapshot(path, source, cancel, deadline)
            _preflight(source, cancel, deadline)
            converted = _convert(directory, cancel, deadline)

            def checked_cancel() -> bool:
                _check(cancel, deadline)
                return False

            document = load_epub(converted, checked_cancel)
            _check(cancel, deadline)
            return Document(path, "text", document.title, document.chapters, sha256=digest)
    except OSError as exc:
        if exc.errno == 28:
            raise ReaderFormatError("临时存储已满，请释放空间后重试") from exc
        raise ReaderFormatError("无法读取或转换 Kindle 图书，请确认文件与临时目录可用") from exc
