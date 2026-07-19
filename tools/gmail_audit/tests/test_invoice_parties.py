"""F1: invoice party (Sprzedawca/Nabywca) NIP extraction for sales-vs-purchase direction."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from document_field_extractor import extract_invoice_parties


def test_invoice_parties_seller_and_buyer() -> None:
    text = (
        "FAKTURA VAT nr FV/1/2026\n"
        "Sprzedawca:\nTOP-INSTAL Sp. z o.o.\nul. Główna 1, 44-310 Radlin\nNIP: 1234567890\n"
        "Nabywca:\nJan Kowalski\nul. Polna 5, 31-000 Kraków\nNIP: 9876543210\n"
        "Razem do zapłaty: 25 000,00 PLN\n"
    )
    parties = extract_invoice_parties(text)
    assert parties.get("seller_nip") == "1234567890"
    assert parties.get("buyer_nip") == "9876543210"


def test_invoice_parties_hyphenated_nip() -> None:
    text = "Sprzedawca ACME NIP 123-456-78-90\nNabywca Klient NIP 987-654-32-10"
    parties = extract_invoice_parties(text)
    assert parties.get("seller_nip") == "1234567890"
    assert parties.get("buyer_nip") == "9876543210"


def test_non_invoice_returns_empty() -> None:
    assert extract_invoice_parties("Dzień dobry, proszę o wycenę pompy ciepła.") == {}
