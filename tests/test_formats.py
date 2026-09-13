import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from typix_reader.formats import (
    CHAPTER_CHARS, LoadCancelled, ReaderFormatError, _xhtml_text,
    file_hash, load_document, read_image,
)


class ReaderFormatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def text(self, value, suffix=".txt"):
        path = self.root / f"book{suffix}"
        path.write_text(value, encoding="utf-8")
        return path

    def epub(self, href="c1.xhtml", source="OPS/c1.xhtml", body=None, container=None):
        path = self.root / "local.epub"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("META-INF/container.xml", container or '''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/content.opf"/></rootfiles></container>''')
            archive.writestr("OPS/content.opf", f'''<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Local Book</dc:title></metadata><manifest><item id="c1" href="{href}" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/></spine></package>''')
            archive.writestr(source, body or '<html><head><title>One</title></head><body><h1>One</h1><p>Hello <em>local</em> reader.</p></body></html>')
        return path

    def test_text_is_split_even_without_paragraph_breaks(self):
        path = self.text("字" * (CHAPTER_CHARS * 2 + 4))
        document = load_document(path)
        self.assertEqual(document.kind, "text")
        self.assertEqual(document.length, 3)
        self.assertTrue(all(len(chapter.text) <= CHAPTER_CHARS for chapter in document.chapters))
        self.assertEqual("".join(c.text for c in document.chapters), "字" * (CHAPTER_CHARS * 2 + 4))
        self.assertEqual(document.sha256, file_hash(path))

    def test_markdown_headings_become_toc(self):
        document = load_document(self.text("# 第一章\n正文一\n\n## 第二章\n正文二", ".markdown"))
        self.assertEqual([c.title for c in document.chapters], ["第一章", "第二章"])

    def test_many_markdown_headings_are_rejected_before_gtk_toc_population(self):
        path = self.text("# x\n" * 20000, ".md")
        with self.assertRaisesRegex(ReaderFormatError, "1000"):
            load_document(path)

    def test_markdown_parser_checks_cancellation_between_headings(self):
        path = self.text("# x\ntext\n" * 100, ".md")
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls > 10

        with self.assertRaises(LoadCancelled):
            load_document(path, cancelled)
        self.assertEqual(calls, 11)

    def test_long_chapter_titles_are_bounded_before_chunk_duplication(self):
        document = load_document(self.text("# " + "长" * (CHAPTER_CHARS * 3), ".md"))
        self.assertEqual(document.length, 4)
        self.assertTrue(all(len(chapter.title) <= 210 for chapter in document.chapters))

    def test_utf8_bom_is_not_part_of_body(self):
        document = load_document(self.text("\ufeff你好世界"))
        self.assertEqual(document.chapters[0].text, "你好世界")

    def test_gb18030_text(self):
        path = self.root / "chinese.txt"
        path.write_bytes("中文阅读".encode("gb18030"))
        self.assertEqual(load_document(path).chapters[0].text, "中文阅读")

    def test_empty_or_binary_text_is_rejected(self):
        for value in (" \n", "a\x00b"):
            with self.subTest(value=repr(value)), self.assertRaises(ReaderFormatError):
                load_document(self.text(value))

    def test_epub_parses_inline_text_in_reading_order(self):
        document = load_document(self.epub())
        self.assertEqual(document.title, "Local Book")
        self.assertEqual(document.chapters[0].title, "One")
        self.assertIn("Hello local reader.", document.chapters[0].text)

    def test_epub_resolves_url_escaped_and_parent_paths_locally(self):
        document = load_document(self.epub(href="../chapter%201.xhtml#body", source="chapter 1.xhtml"))
        self.assertIn("Hello local reader.", document.chapters[0].text)

    def test_malformed_xhtml_preserves_chinese_and_trailing_text(self):
        title, body = _xhtml_text('<body><p>你好 <b>世界</b>！</p>尾声'.encode())
        self.assertIn("你好 世界！", body)
        self.assertTrue(body.endswith("尾声"))
        self.assertEqual(title, "")

    def test_scripts_styles_and_head_are_not_reading_text(self):
        _, body = _xhtml_text(b'<html><head><title>Meta</title><style>secret-style</style></head><body>Hello<script>secret-script</script><p>world</p></body></html>')
        self.assertNotIn("secret", body)
        self.assertNotIn("Meta", body)
        self.assertIn("world", body)

    def test_epub_rejects_external_or_escaping_spine(self):
        for href in ("https://example.test/book.xhtml", "../../private.xhtml", "/etc/passwd"):
            with self.subTest(href=href), self.assertRaises(ReaderFormatError):
                load_document(self.epub(href=href))

    def test_epub_missing_chapter_is_error_not_silent_partial_book(self):
        with self.assertRaises(ReaderFormatError):
            load_document(self.epub(source="OPS/other.xhtml"))

    def test_xml_entity_is_rejected_including_utf16(self):
        value = '<!DOCTYPE container [<!ENTITY x "boom">]><container>&x;</container>'
        for encoding in ("utf-8", "utf-16"):
            with self.subTest(encoding=encoding), self.assertRaises(ReaderFormatError):
                load_document(self.epub(container=value.encode(encoding)))

    def test_cbz_is_lazy_and_naturally_sorted(self):
        path = self.root / "comic.cbz"
        with zipfile.ZipFile(path, "w") as archive:
            for name in ("page10.png", "page2.png", "page1.png"):
                archive.writestr(name, name.encode())
            archive.writestr("__MACOSX/thumb.png", b"ignored")
        document = load_document(path)
        self.assertEqual(document.images, ("page1.png", "page2.png", "page10.png"))
        self.assertEqual(read_image(document, 1), b"page2.png")

    def test_cbz_member_and_expansion_limits(self):
        path = self.root / "big.cbz"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("1.png", b"a" * 128)
        for constant in ("MAX_MEMBER_BYTES", "MAX_EXPANDED_BYTES"):
            with self.subTest(constant=constant), patch(f"typix_reader.formats.{constant}", 64), self.assertRaises(ReaderFormatError):
                load_document(path)

    def test_text_size_limit(self):
        path = self.text("too large")
        with patch("typix_reader.formats.MAX_TEXT_BYTES", 4), self.assertRaises(ReaderFormatError):
            load_document(path)

    def test_file_limit_and_non_regular_paths(self):
        path = self.text("12345")
        with patch("typix_reader.formats.MAX_FILE_BYTES", 4), self.assertRaises(ReaderFormatError):
            load_document(path)
        folder = self.root / "directory.txt"
        folder.mkdir()
        with self.assertRaises(ReaderFormatError):
            load_document(folder)

    def test_cancelled_load_does_not_parse(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(LoadCancelled):
            load_document(self.text("local book"), event.is_set)

    def test_corrupt_and_unsupported_formats_are_friendly_errors(self):
        for suffix in (".epub", ".cbz", ".pdf"):
            with self.subTest(suffix=suffix), self.assertRaises(ReaderFormatError):
                load_document(self.text("broken file", suffix))


if __name__ == "__main__":
    unittest.main()
