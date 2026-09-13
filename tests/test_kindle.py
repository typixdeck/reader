import hashlib
import importlib.util
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from typix_reader import kindle
from typix_reader.formats import LoadCancelled, ReaderFormatError

FIXTURES = Path(__file__).parent / "fixtures" / "kindle"
spec = importlib.util.spec_from_file_location("kindle_fixture_generator", FIXTURES / "make_fixtures.py")
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)
REAL_MOBITOOL = os.environ.get("TYPIX_TEST_MOBITOOL", "/usr/bin/mobitool")


class KindleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def book(self, data=None):
        path = self.root / "book.azw"
        path.write_bytes(data if data is not None else generator.make_book())
        return path

    def preflight(self, data):
        kindle._preflight(self.book(data), None, time.monotonic() + 5)

    def tool(self, body):
        path = self.root / "test-mobitool"
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o700)
        return path

    def conversion(self, tool, cancel=None):
        with patch.object(kindle, "MOBITOOL", str(tool)):
            return kindle.load_kindle(self.book(), cancel)

    def test_original_fixtures_reproduce_exactly(self):
        for name, args in (("paper-boat.mobi", {}), ("paper-boat.prc", {"compression": 2}),
                           ("paper-boat.azw3", {"kf8": True})):
            self.assertEqual((FIXTURES / name).read_bytes(), generator.make_book(**args))

    @unittest.skipUnless(Path(REAL_MOBITOOL).is_file(), "libmobi-tools required for real conversion")
    def test_real_mobi7_palmdoc_and_kf8_with_separate_css_flow(self):
        for name in ("paper-boat.mobi", "paper-boat.prc", "paper-boat.azw3"):
            path = FIXTURES / name
            with self.subTest(name=name), patch.object(kindle, "MOBITOOL", REAL_MOBITOOL):
                result = kindle.load_kindle(path)
                self.assertEqual(result.kind, "text")
                self.assertEqual(result.path, path)
                self.assertEqual(result.sha256, hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertIn("晨光中的纸船", result.title)
                self.assertGreaterEqual(result.length, 2)
                text = "\n".join(c.text for c in result.chapters)
                self.assertIn("清晨，小舟载着一片树叶", text)
                self.assertIn("The paper boat followed the quiet stream.", text)
                self.assertIn("傍晚，我们在岸边把故事读完。", text)
                self.assertLess(text.index("清晨"), text.index("傍晚"))
                self.assertNotIn("color: black", text)

    def test_encryption_v1_and_v2_are_refused_before_tool_runs(self):
        for encryption in (1, 2):
            with self.subTest(encryption=encryption), patch.object(kindle, "_convert") as convert:
                with self.assertRaisesRegex(ReaderFormatError, "DRM"):
                    kindle.load_kindle(self.book(generator.make_book(encryption=encryption)))
                convert.assert_not_called()

    def test_hybrid_secondary_header_encryption_is_also_refused(self):
        first = generator.make_book()
        second = generator.make_book(kf8=True, encryption=1)

        def records(data):
            count = struct.unpack_from(">H", data, 76)[0]
            offsets = [struct.unpack_from(">I", data, 78 + i * 8)[0] for i in range(count)] + [len(data)]
            return [data[a:b] for a, b in zip(offsets, offsets[1:])]

        left, right = records(first), records(second)
        meta = bytearray(left[0])
        generator.word(meta, 128, 0x40)
        generator.word(meta, 84, 304)
        meta[280:280] = b"EXTH" + struct.pack(">IIIII", 24, 1, 121, 12, len(left) + 1)
        left[0] = bytes(meta)
        combined = generator.pack_records(left + [b"BOUNDARY"] + right)
        with self.assertRaisesRegex(ReaderFormatError, "DRM"):
            self.preflight(combined)

    def test_kfx_or_topaz_is_not_misidentified_from_extension(self):
        for magic in (b"TPZ0", b"CONT", b"PK\x03\x04"):
            with self.subTest(magic=magic), self.assertRaisesRegex(ReaderFormatError, "KFX"):
                self.preflight(magic + bytes(100))

    def test_record_table_overlap_and_truncation_rejected(self):
        bad = bytearray(generator.make_book())
        bad[86:90] = bad[78:82]
        with self.assertRaises(ReaderFormatError):
            self.preflight(bad)
        with self.assertRaises(ReaderFormatError):
            self.preflight(generator.make_book()[:90])

    def test_declared_expansion_is_bounded_before_native_parser(self):
        bad = bytearray(generator.make_book())
        offset = struct.unpack_from(">I", bad, 78)[0]
        generator.word(bad, offset + 4, kindle.MAX_TEXT + 1)
        with self.assertRaisesRegex(ReaderFormatError, "32 MB"):
            self.preflight(bad)

    def test_exth_length_cannot_escape_header(self):
        bad = bytearray(generator.make_book())
        offset = struct.unpack_from(">I", bad, 78)[0]
        generator.word(bad, offset + 128, 0x40)
        bad[offset + 280:offset + 292] = b"EXTH" + struct.pack(">II", 0xFFFFFFFF, 1)
        with self.assertRaises(ReaderFormatError):
            self.preflight(bad)

    def test_missing_dependency_has_actionable_error(self):
        with self.assertRaisesRegex(ReaderFormatError, "libmobi-tools"):
            self.conversion(self.root / "absent")

    def test_disk_space_is_checked_before_snapshot_or_converter(self):
        usage = shutil._ntuple_diskusage(100, 100, 0)
        with patch.object(kindle.shutil, "disk_usage", return_value=usage), patch.object(kindle, "_convert") as convert:
            with self.assertRaisesRegex(ReaderFormatError, "存储空间不足"):
                kindle.load_kindle(self.book())
            convert.assert_not_called()

    def test_cancelled_before_work(self):
        with self.assertRaises(LoadCancelled):
            kindle.load_kindle(self.book(), lambda: True)

    def test_cancellation_reaps_real_blocked_converter_and_removes_temp(self):
        marker = self.root / "started"
        tool = self.tool(f"import os,time,pathlib\npathlib.Path({str(marker)!r}).write_text(str(os.getpid())+'\\n'+os.getcwd())\ntime.sleep(20)\n")
        begin = time.monotonic()
        with self.assertRaises(LoadCancelled):
            self.conversion(tool, marker.exists)
        self.assertLess(time.monotonic() - begin, 2)
        pid, folder = marker.read_text().splitlines()
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid), 0)
        self.assertFalse(Path(folder).exists())

    def test_whole_operation_deadline_stops_real_converter(self):
        tool = self.tool("import time\ntime.sleep(20)\n")
        begin = time.monotonic()
        with patch.object(kindle, "TIMEOUT", 0.2), self.assertRaisesRegex(ReaderFormatError, "超时"):
            self.conversion(tool)
        self.assertLess(time.monotonic() - begin, 2)

    def test_shutdown_reaps_daemon_loader_child_and_prevents_later_spawn(self):
        marker = self.root / "started"
        tool = self.tool(f"import os,time,pathlib\npathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(20)\n")
        errors = []

        def load():
            try:
                kindle.load_kindle(path)
            except ReaderFormatError as exc:
                errors.append(exc)

        path = self.book()
        with patch.object(kindle, "_shutting_down", threading.Event()), patch.object(kindle, "MOBITOOL", str(tool)):
            thread = threading.Thread(target=load, daemon=True)
            thread.start()
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            kindle.cancel_active_workers()
            with self.assertRaises(ProcessLookupError):
                os.kill(int(marker.read_text()), 0)
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertTrue(errors)
            with self.assertRaises(LoadCancelled):
                kindle.load_kindle(path)
        self.assertFalse(kindle._workers)

    def test_converter_file_limit_is_kernel_enforced(self):
        tool = self.tool("with open('source.epub','wb') as f:\n    f.write(b'x'*65536)\n")
        with patch.object(kindle, "MAX_OUTPUT", 4096), self.assertRaises(ReaderFormatError):
            self.conversion(tool)

    def test_invalid_output_and_symlink_are_rejected(self):
        for body in ("open('source.epub','wb').write(b'not a zip')\n",
                     "import os\nos.symlink('source.mobi','source.epub')\n"):
            with self.subTest(body=body), self.assertRaises(ReaderFormatError):
                self.conversion(self.tool(body))

    def test_book_is_read_from_snapshot_even_if_original_replaced(self):
        path = self.book()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshot = self.root / "copy.mobi"
        result = kindle._snapshot(path, snapshot, None, time.monotonic() + 5)
        path.write_bytes(b"changed original")
        self.assertEqual(result, digest)
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), digest)

    def test_nonregular_input_does_not_block_open(self):
        fifo = self.root / "fifo.mobi"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ReaderFormatError, "普通图书"):
            kindle.load_kindle(fifo)


if __name__ == "__main__":
    unittest.main()
