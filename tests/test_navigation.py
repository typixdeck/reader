import unittest
from pathlib import Path

from typix_reader.formats import Chapter, Document, LoadCancelled
from typix_reader.navigation import SearchMatch, find_next


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.document = Document(Path("book.md"), "text", "Book", (
            Chapter("One", "你好 Reader. 你好世界"), Chapter("Two", "Second reader chapter."),
        ))

    def test_search_moves_across_chapters(self):
        self.assertEqual(find_next(self.document, "reader", 0, 10), SearchMatch(1, 7, 13))

    def test_search_wraps_and_preserves_unicode_offsets(self):
        self.assertEqual(find_next(self.document, "你好", 1, 20), SearchMatch(0, 0, 2, True))
        self.assertEqual(find_next(self.document, "你好", 0, 2), SearchMatch(0, 11, 13))

    def test_same_chapter_wrap(self):
        single = Document(Path("a.txt"), "text", "a", (Chapter("a", "needle rest"),))
        self.assertEqual(find_next(single, "needle", 0, 6), SearchMatch(0, 0, 6, True))

    def test_query_is_literal_and_no_match_is_safe(self):
        self.assertIsNone(find_next(self.document, ".*", 0, 0))
        self.assertIsNone(find_next(self.document, "", 0, 0))
        self.assertIsNone(find_next(Document(Path("a.cbz"), "image", "a"), "reader", 0, 0))

    def test_search_can_be_cancelled_between_chapters(self):
        with self.assertRaises(LoadCancelled):
            find_next(self.document, "reader", 0, 0, lambda: True)


if __name__ == "__main__":
    unittest.main()
