import base64
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from typix_reader.formats import LoadCancelled, load_document
from typix_reader.opds import Client, Link, OPDSError, MAX_FEED, MAX_DOWNLOAD, TrackedHTTPS, parse_feed, xml_root, expand_search, same_origin, server_url, save_server, load_server

FEED = b'''<feed xmlns="http://www.w3.org/2005/Atom"><title>Calibre Library</title>
<link rel="search" type="application/atom+xml" href="/opds/search?query={searchTerms}"/>
<link rel="next" type="application/atom+xml" href="?page=2"/>
<entry><title>Books</title><content>All books</content><link type="application/atom+xml" href="/books"/></entry>
<entry><title>Test Book</title><author><name>Author</name></author><link rel="http://opds-spec.org/acquisition" type="text/plain" href="/book.txt"/></entry>
<entry><title>Unsupported</title><link rel="http://opds-spec.org/acquisition" type="application/pdf" href="/book.pdf"/></entry></feed>'''


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    observed = []
    def log_message(self, *_args):
        pass
    def do_GET(self):
        self.observed.append(self.path)
        if self.path.startswith("/slow-header"):
            try:
                self.wfile.write(b"HTTP/1.0 200 OK\r\nX-Slow: ")
                self.wfile.flush()
                for _ in range(50):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.05)
                self.wfile.write(b"\r\n\r\n" + FEED)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path.startswith("/basic"):
            expected = "Basic " + base64.b64encode(b"user:secret").decode()
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="books"')
                self.end_headers()
                return
        if self.path.startswith("/digest"):
            auth = self.headers.get("Authorization", "")
            valid = False
            if auth.startswith("Digest "):
                fields = urllib.request.parse_keqv_list(urllib.request.parse_http_list(auth[7:]))
                md5 = lambda value: hashlib.md5(value.encode()).hexdigest()
                expected = md5(f"{md5('user:books:secret')}:nonce:{fields.get('nc')}:{fields.get('cnonce')}:auth:{md5('GET:' + fields.get('uri',''))}")
                valid = fields.get("username") == "user" and fields.get("response") == expected
            if not valid:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Digest realm="books", nonce="nonce", algorithm=MD5, qop="auth"')
                self.end_headers()
                return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port + 1}/steal")
            self.end_headers()
            return
        if self.path == "/throttle":
            self.send_response(429)
            self.send_header("Retry-After", "2")
            self.end_headers()
            return
        payload = b"Chapter one\n\nA local reading fixture." if self.path in {"/book.txt", "/short"} else FEED
        if self.path == "/search.xml":
            payload = b'''<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/"><Url type="application/atom+xml;profile=opds-catalog" template="/opds/search?query={searchTerms}&amp;count={count?}"/></OpenSearchDescription>'''
        self.send_response(200)
        self.send_header("Content-Type", "text/plain" if self.path == "/book.txt" else "application/atom+xml")
        self.send_header("Content-Length", str(MAX_DOWNLOAD + 1 if self.path == "/large" else len(payload) + 10 if self.path == "/short" else len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class OPDSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = Client(self.base + "/opds")

    def test_calibre_navigation_search_pagination_and_formats(self):
        feed = self.client.browse()
        self.assertEqual(feed.title, "Calibre Library")
        self.assertEqual(feed.next, self.base + "/opds?page=2")
        self.assertEqual(feed.entries[0].navigation.url, self.base + "/books")
        self.assertEqual(feed.entries[1].acquisitions[0].extension, ".txt")
        self.assertTrue(feed.entries[2].unsupported)
        found = self.client.search(feed.search, "作者 & name")
        self.assertIn("%26", found.url)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(found.url).query)["query"], ["作者 & name"])

    def test_https_handler_supports_python313_without_legacy_hostname_option(self):
        handler = TrackedHTTPS([])
        if hasattr(handler, "_check_hostname"):
            del handler._check_hostname
        handler.do_open = Mock(return_value="response")
        self.assertEqual(handler.https_open("request"), "response")
        self.assertEqual(handler.do_open.call_args.kwargs, {"context": handler._context})

    def test_opensearch_descriptor_and_calibre_query_links(self):
        link = Link(self.base + "/search.xml", "search", "application/opensearchdescription+xml")
        self.assertIn("query=test", self.client.search(link, "test").url)
        self.assertEqual(expand_search(self.base + "/opds/search?query=&library_id=one", "hello"), self.base + "/opds/search?query=hello&library_id=one")
        with self.assertRaises(OPDSError):
            expand_search(self.base + "/q?x={unknown}&q={searchTerms}", "test")

    def test_basic_and_digest_auth_against_real_http_server(self):
        for mode in ("basic", "digest"):
            with self.subTest(mode=mode):
                client = Client(self.base + f"/{mode}/opds", "user", "secret")
                self.assertEqual(client.browse().title, "Calibre Library")
        with self.assertRaisesRegex(OPDSError, "登录"):
            Client(self.base + "/basic/opds", "user", "wrong").browse()

    def test_cross_origin_acquisition_and_redirect_do_not_send_credentials(self):
        external = f"http://127.0.0.1:{self.server.server_port + 1}/private"
        with self.assertRaises(OPDSError):
            self.client.browse(external)
        with self.assertRaises(OPDSError):
            self.client.browse(self.base + "/redirect")
        with self.assertRaises(OPDSError):
            same_origin("http://host/opds", "https://host/opds")
        with self.assertRaises(OPDSError):
            self.client.download(Link(external, "http://opds-spec.org/acquisition", "text/plain"), self.root, "book")

    def test_server_persistence_contains_no_credentials_or_custom_path_tokens(self):
        path = self.root / "opds.json"
        self.assertTrue(save_server(path, self.base + "/opds"))
        self.assertEqual(json.loads(path.read_text()), {"server": self.base + "/opds"})
        self.assertFalse(save_server(path, self.base + "/api/opds/secret-token"))
        self.assertNotIn("secret-token", path.read_text())
        for url in ["http://user:secret@host/opds", self.base + "/opds?token=secret", "file:///tmp/opds"]:
            with self.assertRaises(OPDSError):
                save_server(path, url)
        path.write_text(json.dumps({"server": self.base + "/api/opds/secret-token"}))
        self.assertEqual(load_server(path), "")

    def test_dtd_entities_encodings_and_resource_limits(self):
        xml = '<!DOCTYPE feed [<!ENTITY x "expanded">]><feed xmlns="http://www.w3.org/2005/Atom"><title>&x;</title></feed>'
        for encoding in ("utf-8", "utf-16", "utf-32"):
            with self.subTest(encoding=encoding), self.assertRaises(OPDSError):
                xml_root(xml.encode(encoding))
        with self.assertRaises(OPDSError):
            xml_root(b"x" * (MAX_FEED + 1))
        with self.assertRaises(OPDSError):
            xml_root(b"<x>" * 40 + b"</x>" * 40)
        with self.assertRaises(OPDSError):
            parse_feed(b'<feed xmlns="http://www.w3.org/2005/Atom">' + b"<entry/>" * 251 + b"</feed>", self.base + "/opds")

    def test_standalone_entry_xml_base_is_applied_once(self):
        feed = parse_feed(b'''<entry xmlns="http://www.w3.org/2005/Atom" xml:base="sub/"><title>A</title><link rel="http://opds-spec.org/acquisition" type="text/plain" href="book.txt"/></entry>''', self.base + "/opds/book")
        self.assertEqual(feed.entries[0].acquisitions[0].url, self.base + "/opds/sub/book.txt")

    def test_real_download_enters_existing_local_parser(self):
        link = Link(self.base + "/book.txt", "http://opds-spec.org/acquisition", "text/plain")
        path = self.client.download(link, self.root, "../../Book: Title")
        self.assertEqual(path.parent, self.root)
        self.assertEqual(path.suffix, ".txt")
        self.assertIn("local reading fixture", load_document(path).chapters[0].text)

    def test_download_never_overwrites_customized_existing_book(self):
        link = Link(self.base + "/book.txt", "http://opds-spec.org/acquisition", "text/plain")
        first = self.client.download(link, self.root, "book")
        first.write_text("User edits must survive")
        second = self.client.download(link, self.root, "book")
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_text(), "User edits must survive")

    def test_download_limits_incomplete_files_and_disk_failure_cleanup(self):
        for route in ("/large", "/short"):
            with self.subTest(route=route), self.assertRaises(OPDSError):
                self.client.download(Link(self.base + route, "http://opds-spec.org/acquisition", "text/plain"), self.root, "book")
            self.assertEqual(list(self.root.iterdir()), [])
        with patch("typix_reader.opds.shutil.disk_usage", return_value=type("Disk", (), {"free": 1})()), self.assertRaisesRegex(OPDSError, "空间"):
            self.client.download(Link(self.base + "/book.txt", "http://opds-spec.org/acquisition", "text/plain"), self.root, "book")

    def test_cancel_before_request_and_unsupported_download(self):
        with self.assertRaises(LoadCancelled):
            self.client.browse(cancel=lambda: True)
        with self.assertRaises(OPDSError):
            self.client.download(Link(self.base + "/pdf", "http://opds-spec.org/acquisition", "application/pdf"), self.root, "book")

    def test_slow_header_can_be_cancelled_before_response_returns(self):
        cancelled = threading.Event()
        timer = threading.Timer(0.15, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(LoadCancelled):
                self.client.browse(self.base + "/slow-header", cancelled.is_set)
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            timer.cancel()

    def test_server_backoff_does_not_spin_retry(self):
        with self.assertRaises(OPDSError):
            self.client.browse(self.base + "/throttle")
        count = len(Handler.observed)
        with self.assertRaisesRegex(OPDSError, "频繁"):
            self.client.browse()
        self.assertEqual(len(Handler.observed), count)


if __name__ == "__main__":
    unittest.main()
