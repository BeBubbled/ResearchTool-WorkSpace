import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pypdf import PdfWriter

import pdf_core
from pdf_core import PdfError


class PdfCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def blank_pdf(self, name: str = "paper.pdf", metadata: dict | None = None) -> Path:
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        if metadata:
            writer.add_metadata(metadata)
        path = self.root / name
        with path.open("wb") as handle:
            writer.write(handle)
        return path

    def test_save_upload_accepts_pdf_and_writes_atomically(self) -> None:
        target = self.root / "out" / "a.pdf"
        written = pdf_core.save_pdf_upload(io.BytesIO(b"%PDF-1.7\nbody"), target)
        self.assertEqual(written, 13)
        self.assertEqual(target.read_bytes(), b"%PDF-1.7\nbody")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["a.pdf"])

    def test_header_may_follow_leading_junk_within_first_kilobyte(self) -> None:
        target = self.root / "junk.pdf"
        pdf_core.save_pdf_upload(io.BytesIO(b"\xef\xbb\xbf\r\n%PDF-1.4 rest"), target)
        self.assertTrue(target.is_file())

    def test_rejections_leave_nothing_behind(self) -> None:
        cases = {
            "not_pdf": (b"hello world" * 200, pdf_core.MAX_PDF_BYTES),
            "empty": (b"", pdf_core.MAX_PDF_BYTES),
            "too_large": (b"%PDF-1.4 0123456789", 8),
        }
        for code, (data, limit) in cases.items():
            with self.subTest(code=code):
                target = self.root / f"{code}.pdf"
                with self.assertRaises(PdfError) as caught:
                    pdf_core.save_pdf_upload(io.BytesIO(data), target, max_bytes=limit)
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(target.exists())
                self.assertFalse(any(self.root.glob(f"{code}.pdf*")))
        self.assertIn("100 MB", str(pdf_core.size_error()))

    def test_fingerprint_is_content_based_and_salted(self) -> None:
        first, second = self.root / "1.pdf", self.root / "2.pdf"
        first.write_bytes(b"%PDF-same")
        second.write_bytes(b"%PDF-same")
        self.assertEqual(pdf_core.file_fingerprint(first), pdf_core.file_fingerprint(second))
        self.assertNotEqual(pdf_core.file_fingerprint(first), pdf_core.file_fingerprint(first, "v2\0"))

    def test_clean_title_drops_generic_and_identifier_titles(self) -> None:
        self.assertEqual(pdf_core.clean_title("  A  Real\nTitle — "), "A Real Title")
        for value in ("Untitled", "Microsoft Word", "132a8368-53b6-4006-bfe1-b23ff44ae3ac", "a" * 32, "", None):
            with self.subTest(value=value):
                self.assertIsNone(pdf_core.clean_title(value))

    def test_info_and_title_from_metadata(self) -> None:
        path = self.blank_pdf(metadata={"/Title": "Embedded Title", "/Author": "Alice; Bob and Carol"})
        info = pdf_core.pdf_info(path)
        self.assertEqual(info.title, "Embedded Title")
        self.assertEqual(info.authors, ["Alice", "Bob", "Carol"])
        self.assertEqual(info.page_count, 1)
        self.assertEqual(pdf_core.pdf_title(path), "Embedded Title")
        self.assertIsNone(pdf_core.pdf_title(self.root / "missing.pdf"))

    def test_extract_pages_and_readable_characters(self) -> None:
        pages = pdf_core.extract_pages(self.blank_pdf())
        self.assertEqual(pages, [""])
        self.assertEqual(pdf_core.readable_characters(["a b\nc", "  d "]), 4)


if __name__ == "__main__":
    unittest.main()
