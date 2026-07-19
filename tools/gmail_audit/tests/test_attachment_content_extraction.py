from __future__ import annotations

import sys
import types
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from attachment_content_extraction import extract_attachment_text, inspect_ocr_runtime
from document_parse_contract import DocumentParseResult


def _build_docx_bytes(paragraphs: list[str]) -> bytes:
    xml_body = "".join(
        f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{xml_body}</w:body>"
        "</w:document>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _build_xlsx_bytes(rows: list[list[str]]) -> bytes:
    shared_strings: list[str] = []
    string_indexes: dict[str, int] = {}

    def shared_index(value: str) -> int:
        if value not in string_indexes:
            string_indexes[value] = len(shared_strings)
            shared_strings.append(value)
        return string_indexes[value]

    sheet_rows: list[str] = []
    for row_number, values in enumerate(rows, start=1):
        cells: list[str] = []
        for column_offset, value in enumerate(values):
            column = chr(ord("A") + column_offset)
            idx = shared_index(value)
            cells.append(f'<c r="{column}{row_number}" t="s"><v>{idx}</v></c>')
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')

    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join(f"<si><t>{value}</t></si>" for value in shared_strings)
        + "</sst>"
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(sheet_rows)}</sheetData>"
        "</worksheet>"
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", shared_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()


def _build_minimal_pdf_bytes(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = b"%PDF-1.4\n"
    offsets: list[int] = []
    for obj_number, payload in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{obj_number} 0 obj\n".encode("ascii")
        body += payload + b"\nendobj\n"
    xref_offset = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    body += b"0000000000 65535 f \n"
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n".encode("ascii")
    body += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode(
            "ascii"
        )
    )
    return body


class AttachmentContentExtractionTests(unittest.TestCase):
    def test_inspect_ocr_runtime_reports_ok_with_mocked_dependencies(self) -> None:
        fake_pil = types.ModuleType("PIL")
        fake_pil.__version__ = "10.0.0"
        fake_image = types.ModuleType("PIL.Image")
        fake_pil.Image = fake_image
        fake_pytesseract = types.ModuleType("pytesseract")
        fake_pytesseract.__version__ = "0.3.13"
        fake_pytesseract.pytesseract = types.SimpleNamespace(tesseract_cmd="tesseract")
        fake_pytesseract.TesseractNotFoundError = RuntimeError
        fake_pytesseract.get_tesseract_version = lambda: "5.5.0"

        with mock.patch.dict(
            sys.modules,
            {
                "PIL": fake_pil,
                "PIL.Image": fake_image,
                "pytesseract": fake_pytesseract,
            },
            clear=False,
        ):
            with mock.patch("attachment_content_extraction.shutil.which", return_value="C:/Program Files/Tesseract-OCR/tesseract.exe"):
                result = inspect_ocr_runtime()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["dependencies"]["pillow"], "10.0.0")
        self.assertEqual(result["dependencies"]["pytesseract"], "0.3.13")
        self.assertEqual(result["binary_path"], "C:/Program Files/Tesseract-OCR/tesseract.exe")

    def test_docx_text_is_extracted(self) -> None:
        payload = _build_docx_bytes(["Powierzchnia 180 m2", "Model Panasonic 9 kW"])

        result = extract_attachment_text(
            payload,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            file_name="parametry.docx",
        )

        self.assertEqual(result["extraction_status"], "ok")
        self.assertEqual(result["parser_provenance"], "stdlib")
        self.assertIn("Panasonic 9 kW", result["extracted_text"])

    def test_xlsx_text_is_extracted(self) -> None:
        payload = _build_xlsx_bytes([["Pole", "Wartosc"], ["Powierzchnia", "190 m2"], ["Model", "Panasonic 9 kW"]])

        result = extract_attachment_text(
            payload,
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            file_name="zestawienie.xlsx",
        )

        self.assertEqual(result["extraction_status"], "ok")
        self.assertIn("Powierzchnia | 190 m2", result["extracted_text"])
        self.assertIn("Panasonic 9 kW", result["extracted_text"])

    def test_xls_binary_fallback_extracts_visible_strings(self) -> None:
        payload = b"\x09\x08FakeXLS\x00Powierzchnia 200 m2\x00Jaworzno\x00Panasonic 9 kW"

        result = extract_attachment_text(
            payload,
            mime_type="application/vnd.ms-excel",
            file_name="legacy.xls",
        )

        self.assertEqual(result["extraction_method"], "xls_binary_strings")
        self.assertEqual(result["extraction_status"], "ok")
        self.assertIn("Panasonic 9 kW", result["extracted_text"])

    def test_zip_container_reports_archive_status(self) -> None:
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("readme.txt", "archive payload")

        result = extract_attachment_text(
            buffer.getvalue(),
            mime_type="application/zip",
            file_name="projekt.zip",
        )

        self.assertEqual(result["extraction_status"], "archive_container")
        self.assertEqual(result["parser_provenance"], "zipfile")

    def test_pdf_text_layer_is_extracted_when_pypdf_is_available(self) -> None:
        try:
            import pypdf  # noqa: F401
        except ImportError:
            self.skipTest("pypdf is not installed in this environment")

        result = extract_attachment_text(
            _build_minimal_pdf_bytes("Jaworzno PDF 180 m2"),
            mime_type="application/pdf",
            file_name="charakterystyka.pdf",
        )

        self.assertEqual(result["extraction_status"], "ok")
        self.assertEqual(result["parser_provenance"], "pypdf")
        self.assertIn("Jaworzno PDF 180 m2", result["extracted_text"])

    def test_docling_parser_is_used_for_pdf_when_enabled(self) -> None:
        with mock.patch(
            "document_parse_runtime.parse_with_docling",
            return_value=DocumentParseResult(
                parser_id="docling",
                plain_text="Docling extraction for Panasonic WH-ADC0309K3E5",
                extraction_method="docling_pdf",
                extraction_confidence=0.91,
                extraction_status="ok",
                parser_provenance="docling",
                structured=True,
                metadata={"page_count": 2, "table_count": 1, "warnings": []},
            ),
        ) as extractor:
            result = extract_attachment_text(
                _build_minimal_pdf_bytes("Jaworzno PDF 180 m2"),
                mime_type="application/pdf",
                file_name="charakterystyka.pdf",
                docling_enabled=True,
                docling_options={"max_pages": 20, "timeout_sec": 30},
            )

        extractor.assert_called_once()
        self.assertEqual(result["parser_provenance"], "docling")
        self.assertEqual(result["extraction_method"], "docling_pdf")
        self.assertEqual(result["metadata"]["page_count"], 2)

    def test_docling_failure_falls_back_to_existing_pdf_parser(self) -> None:
        with mock.patch(
            "document_parse_runtime.parse_with_docling",
            return_value=DocumentParseResult(
                parser_id="docling",
                plain_text="",
                extraction_method="docling_error",
                extraction_confidence=0.0,
                extraction_status="failed",
                parser_provenance="docling",
                metadata={"warnings": ["docling parser failed"]},
            ),
        ):
            result = extract_attachment_text(
                _build_minimal_pdf_bytes("Jaworzno PDF 180 m2"),
                mime_type="application/pdf",
                file_name="charakterystyka.pdf",
                docling_enabled=True,
                docling_options={"max_pages": 20, "timeout_sec": 30},
            )

        self.assertIn(result["parser_provenance"], {"pypdf", "pillow_tesseract", "legacy"})
        self.assertTrue(
            "docling parser failed" in " ".join(result["metadata"].get("warnings") or [])
            or result["parser_provenance"] in {"pypdf", "pillow_tesseract"}
        )


if __name__ == "__main__":
    unittest.main()
