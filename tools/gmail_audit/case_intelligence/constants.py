"""Shared constants for case intelligence sub-modules."""
from v2_semantics import CANONICAL_LIFECYCLE_INTENTS


INTELLIGENCE_PRESENCE_MODES = ("silent", "subtle", "standard", "advisory", "strong", "alarm")
INTELLIGENCE_SURFACE_ZONES = ("desk", "day", "case_only", "silent")
INTELLIGENCE_DAY_BUCKETS = ("teraz", "dzisiaj", "w_najblizszym_czasie", "do_obserwacji")
INTELLIGENCE_LIFECYCLE_INTENTS = CANONICAL_LIFECYCLE_INTENTS
INTELLIGENCE_ACTION_TYPES = (
    "answer_customer",
    "call",
    "wait",
    "follow_up_supplier",
    "prepare_offer",
    "ask_for_missing_data",
    "escalate_internal",
    "merge_with_existing_case",
    "split_case_review",
    "move_to_case_only",
    "resolve_note",
    "review_required",
)
RISK_TYPES = (
    "lead_loss_risk",
    "operational_delay_risk",
    "logistics_risk",
    "finance_risk",
    "interpretation_risk",
    "aging_risk",
    "customer_silence_risk",
    "supplier_dependency_risk",
)
RISK_SEVERITIES = ("low", "medium", "high", "critical")

CASE_GUIDANCE_OPERATIONAL_STATUS = (
    "active_review",
    "waiting",
    "blocked",
    "follow_up_needed",
    "ready",
    "stagnating",
    "watching",
)
CASE_GUIDANCE_WAITING_FOR = (
    "none",
    "client",
    "operator",
    "supplier",
    "document",
    "quote",
    "schedule",
    "payment",
    "unknown",
)
CASE_GUIDANCE_MOMENTUM = ("growing", "steady", "slowing", "stalled")
CASE_GUIDANCE_BUSINESS_READINESS = (
    "not_ready",
    "needs_data",
    "ready_for_offer",
    "ready_for_followup",
    "ready_for_close",
)
CASE_GUIDANCE_OPERATOR_ATTENTION = ("watch", "keep_visible", "act_soon", "act_now", "case_only_ok")
CASE_GUIDANCE_SOURCE_MODES = ("llm_reasoned", "fallback", "skipped")

ACTION_TITLE_PL = {
    "answer_customer": "Odpowiedz klientowi",
    "call": "Zadzwoń",
    "wait": "Poczekaj",
    "follow_up_supplier": "Sprawdź temat u dostawcy",
    "prepare_offer": "Przygotuj ofertę lub handoff",
    "ask_for_missing_data": "Poproś o brakujące dane",
    "escalate_internal": "Przekaż dalej wewnętrznie",
    "merge_with_existing_case": "Połącz z istniejącą sprawą",
    "split_case_review": "Sprawdź, czy trzeba rozdzielić sprawę",
    "move_to_case_only": "Zostaw tylko w sprawie",
    "resolve_note": "Wygasz kartkę",
    "review_required": "Wymagana ręczna ocena",
}

ACTION_CHANNEL = {
    "answer_customer": "mail",
    "call": "phone",
    "wait": "none",
    "follow_up_supplier": "phone",
    "prepare_offer": "internal",
    "ask_for_missing_data": "mail",
    "escalate_internal": "internal",
    "merge_with_existing_case": "internal",
    "split_case_review": "internal",
    "move_to_case_only": "none",
    "resolve_note": "none",
    "review_required": "internal",
}

RISK_TYPE_LABELS_PL = {
    "lead_loss_risk": "ryzyko utraty leada",
    "operational_delay_risk": "ryzyko opóźnienia operacyjnego",
    "logistics_risk": "ryzyko logistyczne",
    "finance_risk": "ryzyko finansowe",
    "interpretation_risk": "ryzyko błędnej interpretacji",
    "aging_risk": "ryzyko zalegania",
    "customer_silence_risk": "ryzyko ciszy po stronie klienta",
    "supplier_dependency_risk": "ryzyko zależności od dostawcy",
}

MISSING_INFO_CRITICAL_KEYWORDS = (
    "address",
    "adres",
    "phone",
    "telefon",
    "metra",
    "m2",
    "moc",
    "power",
    "confirmed case",
    "case reference",
)
MISSING_INFO_IMPORTANT_KEYWORDS = (
    "term",
    "termin",
    "delivery",
    "dostaw",
    "payment",
    "platn",
    "service history",
    "supplier",
    "contact",
)
