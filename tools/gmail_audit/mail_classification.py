"""Shared mailbox message classification (export bootstrap + runtime intake)."""

from __future__ import annotations

import re

LOGISTICS_NOISE_TERMS = (
    "allegro", "inpost", "paczkomat", "poczta polska", "kurier", "dhl", "dpd",
    "ups", "fedex", "gls", "tracking", "trackingu", "przesylka", "przesyłka",
    "paczka", "potwierdzenie nadania", "potwierdzenie odbioru", "status przesylki",
    "status przesyłki", "odebrana", "nadana",
)
MARKETING_NOISE_TERMS = (
    "newsletter", "unsubscribe", "wypisz", "promocja", "promocyjna", "webinar",
    "outlet", "black friday", "rabat", "kampania", "marketing", "google ads", "adwords",
)
SYSTEM_NOISE_TERMS = ("social notification", "security alert", "verification code", "kod weryfikacyjny")
SOCIAL_SENDER_TERMS = ("facebookmail", "linkedin", "instagram", "twitter", "x.com")
SYSTEM_SENDER_TERMS = ("no-reply", "noreply", "donotreply", "mailer-daemon", "postmaster")
SUPPLIER_HINT_TERMS = (
    "tadmar", "onninen", "hydrosolar", "ims", "beretta", "panasonic", "stiebel", "bims", "atum",
    "schiessl", "tcl", "climatek",
)
SUPPLIER_OPPORTUNITY_VALUE_TERMS = (
    "rabat", "promocja dla instalator", "dla instalatorów", "dla instalatorow",
    "cennik", "hurtownia", "marża", "marza", "premiera", "gorące ceny", "gorace ceny",
    "nowa promocja", "ekskluzyw", "oferta hurtowa",
)
SUPPLIER_BRAND_NEWSLETTER_SENDERS = (
    "panasonicproclub", "crlsrv.com", "panasonicproclub.com",
)
DOCUMENT_REVIEW_TERMS = (
    "faktura", "fv", "ksef", "kse-f", "invoice", "rachunek", "nota", "platnosc", "płatność",
)
OPERATIONAL_OVERRIDE_TERMS = (
    "lead", "zapytanie", "prosze o oferte", "proszę o ofertę", "wycena", "dobor", "dobór",
    "oferta", "zamowienie", "zamówienie", "serwis", "awaria", "usterka", "naprawa",
    "przeglad", "przegląd", "reklamacja", "gwarancja", "pompa ciepla", "pompa ciepła",
    "projekt", "rzut", "audyt",
)
CIEPLO_DOZORCA_SENDER = "dozorca@cieplo.app"
CIEPLO_ADMIN_SUBJECT_TERMS = (
    "subskrypcja",
    "subscription",
    "admin",
    "newsletter",
    "powiadomienie systemowe",
)


def is_cieplo_dozorca_noise(sender: str, subject: str, body: str = "") -> bool:
    sender_l = (sender or "").strip().lower()
    if CIEPLO_DOZORCA_SENDER not in sender_l:
        return False
    combined = f"{subject or ''} {body or ''}".lower()
    return any(term in combined for term in CIEPLO_ADMIN_SUBJECT_TERMS)


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    t = text.lower()
    return any(n in t for n in needles)


def _is_supplier_brand_newsletter_noise(sender_lower: str, text: str) -> bool:
    if not any(token in sender_lower for token in SUPPLIER_BRAND_NEWSLETTER_SENDERS):
        return False
    return _contains_any(text, MARKETING_NOISE_TERMS) or "unsubscribe" in text or "newsletter" in text.lower()


def _is_valuable_supplier_opportunity(sender_lower: str, text: str, *, is_marketing: bool) -> bool:
    if not is_marketing:
        return False
    if not _contains_any(sender_lower + " " + text, SUPPLIER_HINT_TERMS):
        return False
    return _contains_any(text, SUPPLIER_OPPORTUNITY_VALUE_TERMS)


def classify_message(
    subject: str,
    snippet: str,
    sender: str,
    labels: list,
    body: str,
    has_attachment: bool,
    direction: str,
) -> dict:
    _ = direction  # reserved for future direction-aware routing
    text = " ".join(str(x) for x in [subject, snippet, sender, body] if x).lower()
    sender_lower = (sender or "").lower()
    labels_upper = {str(label).upper() for label in labels} if labels else set()
    exclusion_reasons: list[str] = []
    score = 0.0
    priority_reasons: list[str] = []

    if labels_upper & {"SPAM", "TRASH"}:
        exclusion_reasons.append("spam_or_trash_label")
    if is_cieplo_dozorca_noise(sender, subject, body):
        exclusion_reasons.append("cieplo_dozorca_admin_noise")
    if _is_supplier_brand_newsletter_noise(sender_lower, text):
        exclusion_reasons.append("supplier_brand_newsletter_noise")

    is_logistics = _contains_any(text, LOGISTICS_NOISE_TERMS)
    is_operational = _contains_any(text, OPERATIONAL_OVERRIDE_TERMS)
    if is_logistics and not is_operational:
        exclusion_reasons.append("logistics_tracking_noise")
    if _contains_any(text, ("google ads", "adwords")) and not _contains_any(text, DOCUMENT_REVIEW_TERMS):
        exclusion_reasons.append("google_ads_marketing_noise")
    is_marketing = _contains_any(text, MARKETING_NOISE_TERMS)
    has_document = _contains_any(text, DOCUMENT_REVIEW_TERMS)
    valuable_supplier_opportunity = _is_valuable_supplier_opportunity(
        sender_lower, text, is_marketing=is_marketing
    )
    if is_marketing and not is_operational and not has_document and not valuable_supplier_opportunity:
        if _contains_any(sender_lower + " " + text, SUPPLIER_HINT_TERMS):
            exclusion_reasons.append("supplier_marketing_newsletter")
        else:
            exclusion_reasons.append("newsletter_or_marketing_noise")
    if _contains_any(sender_lower, SYSTEM_SENDER_TERMS) and not has_document:
        exclusion_reasons.append("no_reply_or_system_sender")
    if _contains_any(text, SYSTEM_NOISE_TERMS):
        exclusion_reasons.append("system_noise")
    if _contains_any(sender_lower, SOCIAL_SENDER_TERMS):
        exclusion_reasons.append("social_notification")

    if exclusion_reasons:
        return {
            "candidate": False,
            "score": 0.0,
            "priority_rank": 0,
            "candidate_tier": "noise_excluded",
            "priority_reasons": [],
            "exclusion_reasons": exclusion_reasons,
            "case_type": "noise",
            "is_task": False,
            "priority_label": "pomijany",
            "priority_score": 0,
        }

    if _contains_any(text, ("lead", "zapytanie", "prosze o oferte", "proszę o ofertę", "wycena", "dobor", "dobór", "kalkulacja")):
        priority_reasons.append("active_lead_or_offer")
        score += 4.0
    if _contains_any(text, ("oferta", "pompa ciepla", "pompa ciepła", "klimatyzacja", "klimatyzator", "montaz", "montaż", "instalacja")):
        priority_reasons.append("offer_or_heat_pump")
        score += 3.0
    if _contains_any(text, ("serwis", "awaria", "usterka", "naprawa", "przeglad", "przegląd", "nie dziala", "nie działa", "zgłoszenie")):
        priority_reasons.append("service_or_repair")
        score += 3.0
    if _contains_any(text, ("reklamacja", "gwarancja", "problem")):
        priority_reasons.append("complaint_or_warranty")
        score += 3.0
    if has_document:
        priority_reasons.append("customer_or_finance_document")
        score += 2.0
    if _contains_any(text, ("dokument", "zalacznik", "załącznik", "projekt", "rzut", "audyt")):
        priority_reasons.append("document_to_review")
        score += 1.5
    if has_attachment:
        priority_reasons.append("has_attachments")
        score += 1.5
    if _contains_any(text, ("wspolpraca", "współpraca", "partner", "zatrudnienie", "praca", "oferta współpracy")):
        priority_reasons.append("business_partnership")
        score += 2.0
    if _contains_any(text, ("dofinansowanie", "dotacja", "czyste powietrze", "ulga", "termomodernizacja")):
        priority_reasons.append("funding_or_grant")
        score += 2.0
    if _contains_any(text, ("pozyczka", "pożyczka", "kredyt", "finansowanie", "leasing", "santander", "raty")):
        priority_reasons.append("financing_offer")
        score += 1.0
    if _contains_any(text, ("zus", "podatek", "podatki", "ksiegowosc", "księgowość", "ksiegowa", "księgowa", "bilans", "pit", "vat", "nip")):
        priority_reasons.append("accounting_tax")
        score += 2.0
    if _contains_any(text, ("strona", "www", "google", "pozycjonowanie", "seo", "internet", "witryna", "techniczne")):
        priority_reasons.append("it_website")
        score += 1.0
    if _contains_any(sender_lower, ("gmail.com", "wp.pl", "interia.pl", "onet.pl", "o2.pl")) or "@" not in sender_lower:
        priority_reasons.append("real_customer_sender")
        score += 1.0
    if _contains_any(sender_lower + " " + text, SUPPLIER_HINT_TERMS):
        priority_reasons.append("supplier_or_distributor")
        score += 1.0
    if valuable_supplier_opportunity:
        priority_reasons.append("supplier_opportunity")
        score += 2.5

    if re.search(r"^(re|fw|fwd|odp):?\s", (subject or "").strip().lower()):
        priority_reasons.append("is_reply_or_forward")
        score += 0.5

    if score <= 0.0:
        return {
            "candidate": False,
            "score": 0.0,
            "priority_rank": 0,
            "candidate_tier": "low_value_excluded",
            "priority_reasons": [],
            "exclusion_reasons": ["low_operational_value"],
            "case_type": "unknown_low_value",
            "is_task": False,
            "priority_label": "pomijany",
            "priority_score": 0,
        }

    reasons = set(priority_reasons)
    if "supplier_opportunity" in reasons:
        case_type = "supplier_opportunity"
    elif reasons & {"active_lead_or_offer", "offer_or_heat_pump"}:
        case_type = "lead_oferta"
    elif "service_or_repair" in reasons:
        case_type = "serwis"
    elif "complaint_or_warranty" in reasons:
        case_type = "reklamacja_gwarancja"
    elif "funding_or_grant" in reasons:
        case_type = "dofinansowanie"
    elif "business_partnership" in reasons:
        case_type = "wspolpraca"
    elif "accounting_tax" in reasons:
        case_type = "ksiegowosc_podatki"
    elif "customer_or_finance_document" in reasons or "document_to_review" in reasons:
        case_type = "dokumenty"
    elif "financing_offer" in reasons:
        case_type = "finansowanie"
    elif "it_website" in reasons:
        case_type = "it_strona"
    elif "supplier_or_distributor" in reasons:
        case_type = "dostawca"
    else:
        case_type = "other"

    is_task = bool(
        reasons
        & {
            "active_lead_or_offer",
            "service_or_repair",
            "complaint_or_warranty",
            "business_partnership",
            "document_to_review",
            "accounting_tax",
            "funding_or_grant",
            "supplier_opportunity",
        }
    )

    if score >= 4.0:
        priority_label = "P1 - pilne"
    elif score >= 2.5:
        priority_label = "P2 - ważne"
    elif score >= 1.0:
        priority_label = "P3 - do przejrzenia"
    else:
        priority_label = "P4 - informacja"

    return {
        "candidate": True,
        "score": round(score, 1),
        "priority_rank": min(10, int(score)),
        "candidate_tier": "operational_candidate",
        "priority_reasons": priority_reasons,
        "exclusion_reasons": [],
        "case_type": case_type,
        "is_task": is_task,
        "priority_label": priority_label,
        "priority_score": round(score, 1),
    }


__all__ = ["classify_message", "is_cieplo_dozorca_noise"]
