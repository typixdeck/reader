"""OPDS download error presentation without starting a GUI session."""
import types
import unicodedata
import unittest

from typix_reader.formats import LoadCancelled, ReaderFormatError
from typix_reader.opds import OPDSError

try:
    from typix_reader.opds_ui import OPDSMixin, download_error_message
except (ImportError, ValueError):
    OPDSMixin = None


@unittest.skipIf(OPDSMixin is None, "GTK GI required; tested on CM4")
class DownloadErrorTests(unittest.TestCase):
    def test_parser_reasons_include_encryption_dependency_and_storage(self):
        for reason in ("PDF 已加密或需要密码；请提供拥有权限的未加密副本",
                       "缺少 Kindle 格式组件，请安装 libmobi-tools",
                       "临时存储空间不足，请释放空间后重试"):
            with self.subTest(reason=reason):
                message = download_error_message(ReaderFormatError(reason))
                self.assertIn(reason, message)
                self.assertIn("原有图书与阅读进度保留", message)

    def test_only_expected_errors_are_exposed_as_plain_bounded_text(self):
        message = download_error_message(ReaderFormatError("PDF\x00\x1b\u202e\n已加密" + "很长" * 1000))
        self.assertTrue(message.startswith("PDF 已加密"))
        self.assertLessEqual(len(message), 350)
        self.assertFalse(any(unicodedata.category(c).startswith("C") for c in message.replace("\n", "")))
        self.assertIn("登录失败", download_error_message(OPDSError("登录失败")))
        secret = "https://private:password@example.test/book?token=secret /private/home/path"
        for error in (OSError(secret), RuntimeError(secret), ValueError(secret)):
            message = download_error_message(error)
            self.assertNotIn("secret", message)
            self.assertNotIn("private", message)
            self.assertIn("原有图书", message)

    def test_failed_download_parse_preserves_document_and_position(self):
        old_document = object()
        host = types.SimpleNamespace(
            document=old_document, position=7, state=object(), messages=[],
            opds_client=object(), status_label=types.SimpleNamespace(set_text=lambda value: None),
            opds_set_busy=lambda busy: None,
        )
        host.message = host.messages.append
        loaded = []
        host.document_loaded = lambda *args: loaded.append(args)
        host.submit = lambda _work, done: done(None, ReaderFormatError("PDF 已加密或需要密码"))
        OPDSMixin.opds_download(host, object(), object())
        self.assertIs(host.document, old_document)
        self.assertEqual(host.position, 7)
        self.assertFalse(loaded)
        self.assertIn("PDF 已加密或需要密码", host.messages[0])

    def test_cancellation_does_not_display_warning_or_replace_old_book(self):
        host = types.SimpleNamespace(
            opds_client=object(), status_label=types.SimpleNamespace(set_text=lambda value: None),
            opds_set_busy=lambda busy: None, messages=[],
        )
        host.message = host.messages.append
        host.submit = lambda _work, done: done(None, LoadCancelled("已取消打开"))
        OPDSMixin.opds_download(host, object(), object())
        self.assertFalse(host.messages)


if __name__ == "__main__":
    unittest.main()
