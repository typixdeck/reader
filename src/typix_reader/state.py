"""Private local reading history, compatible with the original state format."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_RECENT = 20
MAX_POSITIONS = 200


@dataclass
class ReadingPosition:
    document_sha256: str
    chapter: int = 0
    offset: int = 0  # Character offset at the top of the text viewport.


@dataclass
class ReaderState:
    recent: list[str] = field(default_factory=list)
    positions: dict[str, ReadingPosition] = field(default_factory=dict)
    books: dict[str, dict] = field(default_factory=dict)
    font_size: int = 20

    def remember(self, path: Path, document_sha256: str, chapter: int = 0, offset: int = 0,
                 title: str = "", length: int = 0) -> None:
        value = str(path.resolve())
        self.recent = [value] + [item for item in self.recent if item != value]
        self.recent = self.recent[:MAX_RECENT]
        self.positions.pop(document_sha256, None)
        self.positions[document_sha256] = ReadingPosition(document_sha256, max(0, chapter), max(0, offset))
        self.positions = dict(list(self.positions.items())[-MAX_POSITIONS:])
        self.books[value] = {"title": title or path.stem, "sha256": document_sha256, "length": length}
        self.books = {name: self.books[name] for name in self.recent if name in self.books}

    def position_for(self, document_sha256: str) -> ReadingPosition | None:
        return self.positions.get(document_sha256)


def state_path() -> Path:
    return Path(os.environ.get("TYPIX_READER_STATE", Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")
    ) / "typix-reader" / "state.json"))


def _integer(value: object, default: int = 0, maximum: int = 100000000) -> int:
    if type(value) is not int:
        return default
    return min(maximum, max(0, value))


def load_state(path: Path | None = None) -> ReaderState:
    path = path or state_path()
    try:
        if path.stat().st_size > 1024 * 1024:
            return ReaderState()
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError, RecursionError):
        return ReaderState()
    if not isinstance(raw, dict):
        return ReaderState()
    state = ReaderState()
    recent = raw.get("recent", [])
    if isinstance(recent, list):
        state.recent = list(dict.fromkeys(item for item in recent if isinstance(item, str)))[:MAX_RECENT]
    positions = raw.get("positions", {})
    if isinstance(positions, dict):
        for digest, value in list(positions.items())[-MAX_POSITIONS:]:
            if isinstance(digest, str) and isinstance(value, dict):
                state.positions[digest] = ReadingPosition(digest, _integer(value.get("chapter")), _integer(value.get("offset")))
    books = raw.get("books", {})
    if isinstance(books, dict):
        for name in state.recent:
            item = books.get(name)
            if isinstance(item, dict) and isinstance(item.get("title"), str) and isinstance(item.get("sha256"), str):
                state.books[name] = {"title": item["title"], "sha256": item["sha256"], "length": _integer(item.get("length"))}
    state.font_size = max(16, min(34, _integer(raw.get("font_size"), 20, 34)))
    return state


def save_state(state: ReaderState, path: Path | None = None) -> None:
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".reader-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 2, "recent": state.recent, "positions": {
                digest: asdict(value) for digest, value in state.positions.items()
            }, "books": state.books, "font_size": state.font_size}, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
