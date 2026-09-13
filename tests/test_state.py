import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typix_reader.state import MAX_POSITIONS, ReadingPosition, ReaderState, load_state, save_state


class ReaderStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "state.json"

    def test_recent_progress_titles_and_font_roundtrip(self):
        state = ReaderState(font_size=24)
        first, second = Path("/tmp/first.epub"), Path("/tmp/second.cbz")
        state.remember(first, "a" * 64, 2, 100, "First", 5)
        state.remember(second, "b" * 64, 4, 0, "Comic", 12)
        save_state(state, self.path)
        restored = load_state(self.path)
        self.assertEqual(restored.recent, [str(second.resolve()), str(first.resolve())])
        self.assertEqual(restored.position_for("a" * 64), ReadingPosition("a" * 64, 2, 100))
        self.assertEqual(restored.position_for("b" * 64).chapter, 4)
        self.assertEqual(restored.font_size, 24)
        self.assertEqual(restored.books[str(first.resolve())]["title"], "First")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_original_state_format_migrates_without_losing_progress(self):
        self.path.write_text(json.dumps({"recent": ["/tmp/book.epub"], "positions": {"abc": {"chapter": 3, "offset": 200}}}))
        restored = load_state(self.path)
        self.assertEqual(restored.recent, ["/tmp/book.epub"])
        self.assertEqual(restored.position_for("abc").offset, 200)
        self.assertEqual(restored.font_size, 20)

    def test_malformed_state_is_ignored_without_crashing(self):
        values = ("broken json", "[]", "null", '{"recent": null, "positions": []}',
                  '{"positions": {"a": {"chapter": "x", "offset": null}}, "font_size": false}')
        for value in values:
            with self.subTest(value=value):
                self.path.write_text(value)
                state = load_state(self.path)
                self.assertIsInstance(state, ReaderState)
                self.assertEqual(state.font_size, 20)

    def test_invalid_utf8_and_large_state_are_recoverable(self):
        for value in (b"\xff", b" " * (1024 * 1024 + 1), b"[" * 2000 + b"]" * 2000):
            self.path.write_bytes(value)
            self.assertEqual(load_state(self.path), ReaderState())

    def test_history_and_positions_are_bounded_and_duplicates_move_to_front(self):
        state = ReaderState()
        for index in range(MAX_POSITIONS + 10):
            state.remember(Path(f"/tmp/{index}.txt"), str(index), index)
        self.assertEqual(len(state.recent), 20)
        self.assertEqual(len(state.positions), MAX_POSITIONS)
        self.assertEqual(len(state.books), 20)
        state.remember(Path("/tmp/205.txt"), "205", 4)
        self.assertTrue(state.recent[0].endswith("205.txt"))
        self.assertEqual(len(state.recent), 20)

    def test_disk_failure_preserves_original_file_and_cleans_temporary(self):
        initial = ReaderState(font_size=22)
        save_state(initial, self.path)
        original = self.path.read_bytes()
        with patch("typix_reader.state.os.replace", side_effect=OSError("No space left on device")):
            with self.assertRaises(OSError):
                save_state(ReaderState(font_size=30), self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".reader-*.tmp")), [])

    def test_font_and_progress_are_clamped(self):
        self.path.write_text(json.dumps({"font_size": 900, "positions": {"a": {"chapter": -2, "offset": -1}}}))
        state = load_state(self.path)
        self.assertEqual(state.font_size, 34)
        self.assertEqual(state.positions["a"].chapter, 0)
        self.assertEqual(state.positions["a"].offset, 0)


if __name__ == "__main__":
    unittest.main()
