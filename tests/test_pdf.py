import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pdf_fixture import write_pdf
from typix_reader.formats import LoadCancelled, ReaderFormatError, load_document
from typix_reader.pdf_backend import load_pdf, render_pdf, search_pdf, request, stop_pdf_workers
from typix_reader.pdf_worker import render_geometry, MAX_PIXELS, MAX_DIMENSION


def native_available():
    try:
        import gi
        gi.require_version("Poppler", "0.18")
        gi.require_foreign("cairo")
        from gi.repository import Poppler
        return True
    except (ImportError, ValueError):
        return False


class PDFBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path=write_pdf(Path(self.directory.name)/'original.pdf')

    def test_render_geometry_bounds_pixels_dimensions_and_bad_values(self):
        for size in [(420,595),(14400,1),(1,14400),(14400,14400)]:
            width,height,scale=render_geometry(*size,2048,2048,4)
            self.assertLessEqual(width,MAX_DIMENSION)
            self.assertLessEqual(height,MAX_DIMENSION)
            self.assertLessEqual(width*height,MAX_PIXELS)
            self.assertGreater(scale,0)
        for size in [(0,500),(float('nan'),500),(500,14401)]:
            with self.assertRaises(ValueError): render_geometry(*size,800,600,1)

    def test_cancel_before_open_and_nonregular_or_oversized_file(self):
        with self.assertRaises(LoadCancelled): load_pdf(self.path,lambda:True)
        with self.assertRaises(ReaderFormatError): request(self.path.parent,'metadata')
        with self.path.open('wb') as stream: stream.truncate(128*1024**2+1)
        with self.assertRaisesRegex(ReaderFormatError,'128'): load_pdf(self.path)

    def test_native_worker_cancel_and_timeout_are_reaped(self):
        real_popen=subprocess.Popen
        processes=[]
        def sleeping(_command,**kwargs):
            process=real_popen([sys.executable,'-c','import time; time.sleep(10)'],**kwargs)
            processes.append(process)
            return process
        started=time.monotonic()
        with patch('typix_reader.pdf_backend.subprocess.Popen',side_effect=sleeping):
            with self.assertRaises(LoadCancelled):
                request(self.path,'metadata',lambda:time.monotonic()-started>.12)
            with self.assertRaisesRegex(ReaderFormatError,'超时'):
                request(self.path,'metadata',timeout=.05)
        self.assertLess(time.monotonic()-started,2)
        self.assertTrue(all(process.poll() is not None for process in processes))

    def test_low_temporary_storage_is_a_recoverable_error(self):
        with patch('typix_reader.pdf_backend.tempfile.TemporaryDirectory',side_effect=OSError('full')):
            with self.assertRaisesRegex(ReaderFormatError,'空间'): load_pdf(self.path)

    def test_application_shutdown_reaps_native_worker(self):
        real_popen=subprocess.Popen
        ready=threading.Event();processes=[]
        def sleeping(_command,**kwargs):
            process=real_popen([sys.executable,'-c','import time; time.sleep(10)'],**kwargs)
            processes.append(process);ready.set()
            return process
        def invoke():
            try: request(self.path,'metadata')
            except ReaderFormatError: pass
        with patch('typix_reader.pdf_backend.subprocess.Popen',side_effect=sleeping):
            thread=threading.Thread(target=invoke);thread.start()
            self.assertTrue(ready.wait(2))
            stop_pdf_workers();thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(processes[0].poll())


@unittest.skipUnless(native_available(),'Poppler GI/Cairo required; exercised on CM4')
class PDFNativeTests(unittest.TestCase):
    setUp = PDFBoundaryTests.setUp
    def test_original_document_has_pages_and_real_vector_pixels(self):
        document=load_document(self.path)
        self.assertEqual((document.kind,document.length),('pdf',3))
        rendered=render_pdf(document,0,420,595)
        self.assertEqual(rendered['png'][:8],b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II',rendered['png'][16:24]),(420,595))
        import cairo,io
        surface=cairo.ImageSurface.create_from_png(io.BytesIO(rendered['png']))
        data=bytes(surface.get_data());stride=surface.get_stride()
        self.assertNotEqual(data[120*stride+100*4:120*stride+100*4+3],b'\xff\xff\xff')
        next_page=render_pdf(document,1,420,595)
        self.assertNotEqual(rendered['png'],next_page['png'])

    def test_zoom_and_search_move_across_pages_then_wrap(self):
        document=load_pdf(self.path)
        match=search_pdf(document,'lighthouse')
        self.assertEqual(match['page'],1)
        second=search_pdf(document,'lighthouse',match['page'],match['index'])
        self.assertEqual(second['page'],2)
        wrapped=search_pdf(document,'lighthouse',second['page'],second['index'])
        self.assertEqual(wrapped['page'],1);self.assertTrue(wrapped['wrapped'])
        self.assertIsNone(search_pdf(document,'no such phrase'))
        plain=render_pdf(document,1,420,595)
        highlighted=render_pdf(document,1,420,595,match=match['rect'])
        self.assertNotEqual(plain['png'],highlighted['png'])
        zoom=render_pdf(document,1,420,595,zoom=2)
        self.assertEqual((zoom['width'],zoom['height']),(840,1190))

    def test_corrupt_encrypted_and_excess_pages_are_explicit_errors(self):
        self.path.write_bytes(b'%PDF-1.4\nbroken')
        with self.assertRaisesRegex(ReaderFormatError,'损坏'): load_pdf(self.path)
        write_pdf(self.path,encrypted=True)
        with self.assertRaisesRegex(ReaderFormatError,'加密|密码'): load_pdf(self.path)
        write_pdf(self.path,pages=[('Page','Original')]*1001)
        with self.assertRaisesRegex(ReaderFormatError,'1000'): load_pdf(self.path)

    def test_replaced_pdf_is_not_silently_rendered(self):
        document=load_pdf(self.path)
        self.path.write_bytes(self.path.read_bytes()+b'\n% changed')
        with self.assertRaisesRegex(ReaderFormatError,'修改|替换'): render_pdf(document,0,800,600)


if __name__=='__main__': unittest.main()
