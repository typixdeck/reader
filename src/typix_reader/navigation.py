"""GTK-independent book search and location validation."""
from dataclasses import dataclass

from .formats import CancelCheck, Document, LoadCancelled


@dataclass(frozen=True)
class SearchMatch:
    chapter: int
    start: int
    end: int
    wrapped: bool = False


def find_next(document: Document, query: str, chapter: int, offset: int, cancel: CancelCheck = None) -> SearchMatch | None:
    if document.kind != "text" or not query or not document.chapters:
        return None
    # regex preserves original character offsets for Unicode case-insensitive matches.
    import re
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    chapter = max(0, min(chapter, len(document.chapters) - 1))
    ranges = [(chapter, offset, None, False)]
    ranges += [(i, 0, None, i < chapter) for i in list(range(chapter + 1, len(document.chapters))) + list(range(chapter))]
    ranges.append((chapter, 0, offset, True))
    for index, start, end, wrapped in ranges:
        if cancel and cancel():
            raise LoadCancelled("已取消搜索")
        text = document.chapters[index].text
        match = pattern.search(text, min(max(0, start), len(text)), len(text) if end is None else min(end, len(text)))
        if match:
            return SearchMatch(index, match.start(), match.end(), wrapped)
    return None
