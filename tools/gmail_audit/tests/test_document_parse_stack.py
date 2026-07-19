"""Tests for structure-first document parse stack."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from document_field_extractor import extract_structured_fields, structured_fields_to_fact_rows
from document_parse_contract import DocumentElement, DocumentParseConfig, DocumentParseResult, should_skip_regex_document_facts
from document_parse_runtime import parse_document, resolve_parser_chain_from_env
from mailbox_memory.facts import extract_facts_from_text


class ResolveParserChainTest(unittest.TestCase):
    def test_default_chain(self) -> None:
        chain = resolve_parser_chain_from_env("")
        self.assertEqual(chain[0], "docling")
        self.assertIn("legacy", chain)

    def test_custom_chain(self) -> None:
        chain = resolve_parser_chain_from_env("legacy,docling")
        self.assertEqual(chain[0], "legacy")


class StructuredFieldExtractionTest(unittest.TestCase):
    def _parse_text(self, text: str) -> DocumentParseResult:
        return DocumentParseResult(
            parser_id="docling",
            plain_text=text,
            elements=[],
            extraction_method="docling_pdf",
            extraction_confidence=0.9,
            extraction_status="ok",
            structured=True,
        )

    def test_regon_labeled_not_phone(self) -> None:
        text = "REGON: 240318762\nKRS: 000025301\n"
        fields = extract_structured_fields(self._parse_text(text), source_id="doc_test")
        keys = {f["field_name"] for f in fields}
        self.assertIn("regon", keys)
        self.assertNotIn("customer_phone", keys)

    def test_tel_labeled_phone(self) -> None:
        text = "tel.: 327314100\n"
        fields = extract_structured_fields(self._parse_text(text), source_id="doc_test")
        phones = [f for f in fields if f["field_name"] == "customer_phone"]
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0]["field_value"], "327314100")

    def test_vat_table_row_not_city(self) -> None:
        text = "| Wartość VAT | 23% |\n| Miasto | Kraków |\n"
        fields = extract_structured_fields(self._parse_text(text), source_id="doc_test")
        cities = [f for f in fields if f["field_name"] == "city"]
        self.assertEqual(len(cities), 1)
        self.assertEqual(cities[0]["field_value"], "Kraków")

    def test_markdown_kv_nip(self) -> None:
        text = "NIP: 123-456-32-18\n"
        fields = extract_structured_fields(self._parse_text(text), source_id="doc_test")
        self.assertTrue(any(f["field_name"] == "nip" for f in fields))

    def test_pipe_footer_tel_and_city(self) -> None:
        text = "ul. Graniczna 82, 44-178 Przyszowice|tel. 327314100 |www.bimsplus.pl | przyszowice@bimsplus.com.pl\n"
        fields = extract_structured_fields(self._parse_text(text), source_id="doc_test")
        keys = {f["field_name"] for f in fields}
        self.assertIn("customer_phone", keys)
        self.assertIn("city", keys)
        self.assertIn("postal_code", keys)
        self.assertIn("address", keys)
        phones = [f for f in fields if f["field_name"] == "customer_phone"]
        self.assertEqual(phones[0]["field_value"], "327314100")
        cities = [f for f in fields if f["field_name"] == "city"]
        self.assertEqual(cities[0]["field_value"], "Przyszowice")

    def test_inline_regon_line(self) -> None:
        text = "REGON: 240318762\n"
        fields = extract_structured_fields(self._parse_text(text), source_id="doc_test")
        regon = [f for f in fields if f["field_name"] == "regon"]
        self.assertEqual(len(regon), 1)
        self.assertEqual(regon[0]["field_value"], "240318762")
        self.assertNotIn("customer_phone", {f["field_name"] for f in fields})

    def test_loose_tel_label_next_line(self) -> None:
        text = "TEL .:\n327314100\n"
        fields = extract_structured_fields(self._parse_text(text), source_id="doc_test")
        phones = [f for f in fields if f["field_name"] == "customer_phone"]
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0]["field_value"], "327314100")


class RegexGateTest(unittest.TestCase):
    def test_skip_regex_when_structured_docling(self) -> None:
        result = DocumentParseResult(parser_id="docling", plain_text="x", structured=True)
        self.assertTrue(should_skip_regex_document_facts(result))

    def test_allow_regex_when_legacy_unstructured(self) -> None:
        result = DocumentParseResult(parser_id="legacy", plain_text="tel 600700800", structured=False)
        self.assertFalse(should_skip_regex_document_facts(result))

    def test_regex_still_finds_phone_in_message(self) -> None:
        facts = extract_facts_from_text(
            case_id="c1",
            message_id="m1",
            document_id="",
            text="Tel. 600 700 800",
            source_type="message",
            source_ref="m1",
            observed_at="2026-06-01T00:00:00+00:00",
            entity_scope="customer",
            metadata={},
        )
        self.assertTrue(any(f["fact_key"] == "customer_phone" for f in facts))


class LegacyParseTest(unittest.TestCase):
    def test_docx_legacy_chain(self) -> None:
        from tests.test_attachment_content_extraction import _build_docx_bytes

        data = _build_docx_bytes(["NIP: 9998887776", "Miasto: Gdańsk"])
        config = DocumentParseConfig(parser_chain=("legacy",), docling_enabled=False)
        result = parse_document(data, mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", file_name="a.docx", config=config)
        self.assertIn("9998887776", result.plain_text)


class StructuredFactsToRowsTest(unittest.TestCase):
    def test_fact_row_provenance(self) -> None:
        fields = [
            {
                "field_name": "nip",
                "field_value": "1234563218",
                "field_type": "company",
                "confidence": 0.88,
                "evidence_ref": {"source_id": "doc_x", "page": 1, "excerpt": "NIP: 1234563218"},
            }
        ]
        rows = structured_fields_to_fact_rows(
            fields,
            case_id="case_a",
            document_id="doc_x",
            parser_id="docling",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_type"], "structured_document_parse")
        self.assertEqual(rows[0]["fact_key"], "nip")


if __name__ == "__main__":
    unittest.main()
