"""Unit tests for regex fact extraction from message text."""

from __future__ import annotations

import unittest

from mailbox_memory.facts import extract_facts_from_text


def _extract(text: str) -> list[dict]:
    return extract_facts_from_text(
        case_id="case_test",
        message_id="msg_test",
        document_id="",
        text=text,
        source_type="message",
        source_ref="msg_test",
        observed_at="2026-05-23T12:00:00+00:00",
        entity_scope="customer",
        metadata={"origin": "test"},
    )


def _fact_value(facts: list[dict], fact_key: str) -> str | None:
    for row in facts:
        if row.get("fact_key") == fact_key:
            return str(row.get("normalized_value") or "")
    return None


def _fact_confidence(facts: list[dict], fact_key: str) -> float | None:
    for row in facts:
        if row.get("fact_key") == fact_key:
            return float(row["confidence"])
    return None


class ExtractBuildingTypeFactsTest(unittest.TestCase):
    def test_single_family_house_explicit(self) -> None:
        facts = _extract("Mam dom jednorodzinny o powierzchni 150m²")
        self.assertEqual(_fact_value(facts, "building_type"), "single_family_house")
        self.assertGreaterEqual(_fact_confidence(facts, "building_type") or 0, 0.7)

    def test_semi_detached(self) -> None:
        facts = _extract("Mieszkam w bliźniaku")
        self.assertEqual(_fact_value(facts, "building_type"), "semi_detached")

    def test_terraced(self) -> None:
        facts = _extract("Szeregowiec, rok budowy 2005")
        self.assertEqual(_fact_value(facts, "building_type"), "terraced")

    def test_apartment(self) -> None:
        facts = _extract("Mieszkanie na 3 piętrze")
        self.assertEqual(_fact_value(facts, "building_type"), "apartment")

    def test_no_building_type_on_generic_inquiry(self) -> None:
        facts = _extract("Hej, chciałbym wycenę")
        self.assertIsNone(_fact_value(facts, "building_type"))


class ExtractPowerKwFactsTest(unittest.TestCase):
    def test_pompa_glued_kw(self) -> None:
        facts = _extract("Potrzebuję pompę 12kW")
        self.assertEqual(_fact_value(facts, "power_kw"), "12.0")

    def test_zapotrzebowanie_spaced_kw(self) -> None:
        facts = _extract("Zapotrzebowanie wynosi 9 kW")
        self.assertEqual(_fact_value(facts, "power_kw"), "9.0")

    def test_moc_grzewcza_kilowat(self) -> None:
        facts = _extract("moc grzewcza 15 kilowat")
        self.assertEqual(_fact_value(facts, "power_kw"), "15.0")

    def test_voltage_not_power_kw(self) -> None:
        facts = _extract("mam 230V w domu")
        self.assertIsNone(_fact_value(facts, "power_kw"))

    def test_over_100kw_ignored(self) -> None:
        facts = _extract("pompa 150kW dla hali")
        self.assertIsNone(_fact_value(facts, "power_kw"))


class ExtractPhoneFactsTest(unittest.TestCase):
    def test_regon_in_footer_not_phone(self) -> None:
        text = "NIP 2220805221 | KRS 0000253014 | Regon 240318762 | www.bimsplus.com.pl"
        facts = _extract(text)
        self.assertIsNone(_fact_value(facts, "customer_phone"))

    def test_krs_fragment_not_phone(self) -> None:
        facts = _extract("KRS 0000253014")
        self.assertIsNone(_fact_value(facts, "customer_phone"))

    def test_real_tel_still_extracted(self) -> None:
        facts = _extract("tel. 327314100")
        self.assertEqual(_fact_value(facts, "customer_phone"), "327314100")

    def test_offer_reference_hyphen_not_phone(self) -> None:
        facts = _extract("OFERTA WYPRZEDAŻOWA 35631341-001")
        self.assertIsNone(_fact_value(facts, "customer_phone"))


class ExtractFactsCombinedTest(unittest.TestCase):
    def test_hvac_mail_extracts_area_building_and_power(self) -> None:
        text = (
            "Dzień dobry, mam dom jednorodzinny 150 m², węgiel. "
            "Proszę o wycenę pompy ciepła ok. 12 kW. Tel. 600 700 800."
        )
        facts = _extract(text)
        self.assertEqual(_fact_value(facts, "building_type"), "single_family_house")
        self.assertEqual(_fact_value(facts, "power_kw"), "12.0")
        self.assertIsNotNone(_fact_value(facts, "heated_area_m2"))
        self.assertIsNotNone(_fact_value(facts, "customer_phone"))


if __name__ == "__main__":
    unittest.main()
