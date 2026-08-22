"""Customer-facing draft sanity gate (PLANNER-EXEC-FIDELITY-01).

Deterministic, cheap invariants applied to every generated customer draft
before it can become an enabled final action.
"""

from __future__ import annotations

import re
from typing import Any

from agent_runtime.known_fact_guard import (
    is_service_case_kind,
    known_facts_from_snapshot,
)

_PLACEHOLDER_RE = re.compile(
    r"(\{\{[^}]+\}\}|\[TODO\]|\[placeholder\]|TBD_INTERNAL|__INTERNAL__)",
    re.IGNORECASE,
)
_SALES_ASK_RE = re.compile(
    r"\b(metr(a[zż]|aż|azu)|ozc|powierzchni|wycen|ofert)\b",
    re.IGNORECASE,
)
_PROMISE_RE = re.compile(
    r"\b(gwarantujemy|na pewno zainstalujemy|wysyłamy ofertę dzisiaj|"
    r"cena ostateczna wynosi)\b",
    re.IGNORECASE,
)
_SERVICE_CLARIFICATION_RE = re.compile(
    r"\b(model\w*|urz[aą]dzeni\w*|objaw\w*|kod\w*\s+b[łl]?[ęe]d\w*|komunikat\w*|zdj[eę]ci\w*|"
    r"opis\s+awarii|opis\s+usterki)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_SERVICE_PROMISE_RE = re.compile(
    r"\b(technik|serwisant).{0,80}\b(przyjedzie|b[eę]dzie|wyjedzie)\b|"
    r"\b(um[oó]wimy\s+wizyt[eę]|ustalimy\s+termin\s+wizyty|"
    r"termin\s+zosta[łl]\s+ustalony|zadzwoni(?:my)?\s+jutro)\b",
    re.IGNORECASE | re.DOTALL,
)
_DIAGNOSIS_CERTAINTY_RE = re.compile(
    r"\b(to\s+na\s+pewno|na\s+pewno.{0,60}(uszkod|awari|czujnik)|"
    r"uszkodzony\s+czujnik)\b",
    re.IGNORECASE | re.DOTALL,
)
# P1.4: past-tense execution claims (scheduling/sending done) and completion
# claims are only legal with real execution evidence, which this slice never
# has. Any such wording in a customer draft fails closed.
_INTENT_EXECUTION_CLAIM_RE = re.compile(
    r"\b(um\u00f3wi(?:li\u015bmy|li|\u0142em|\u0142).{0,24}(wizyt\w*|przegl\u0105d\w*|termin\w*)|"
    r"zosta(?:\u0142a|\u0142|\u0142o).{0,24}(um\u00f3wiona|um\u00f3wiony|"
    r"wys\u0142ana|wys\u0142any|przes\u0142ana|przes\u0142any)|"
    r"(wys\u0142ali\u015bmy|przes\u0142ali\u015bmy|wys\u0142ali|przes\u0142ali).{0,40}"
    r"(faktur\w*|dokument\w*|mail\w*|e-mail\w*)|"
    r"(wizyt\w*|przegl\u0105d\w*).{0,24}(potwierdzon\w*|zarezerwowan\w*))\b",
    re.IGNORECASE | re.DOTALL,
)
_INTENT_COMPLETION_CLAIM_RE = re.compile(
    r"\b(problem (zosta\u0142|zosta\u0142a) rozwi\u0105zany|"
    r"sprawa (zosta\u0142a) zamkni\u0119ta|"
    r"wszystko (zosta\u0142o) za\u0142atwione|"
    r"sprawa (zosta\u0142a) zako\u0144czona)\b",
    re.IGNORECASE | re.DOTALL,
)


def _intent_coverage_issues(
    *,
    coverage: dict[str, Any] | None,
    body: str,
) -> list[str]:
    """P1.4 structural intent coverage checks (deterministic, no LLM)."""
    if not isinstance(coverage, dict) or not coverage:
        return []
    reasons: list[str] = []
    intent_ids = [str(x) for x in (coverage.get("intent_ids") or []) if str(x)]
    covered = {str(x) for x in (coverage.get("covered_intent_ids") or []) if str(x)}
    unresolved = {str(x) for x in (coverage.get("unresolved_intent_ids") or []) if str(x)}
    ignored = {str(x) for x in (coverage.get("ignored_intent_ids") or []) if str(x)}
    known = covered | unresolved | ignored
    if not intent_ids:
        return []
    if ignored or known != set(intent_ids):
        reasons.append("MULTI_INTENT_DROPPED")

    required_by = coverage.get("required_information_by_intent")
    requested_by = coverage.get("requested_information_by_intent")
    if isinstance(required_by, dict) and isinstance(requested_by, dict):
        requested_all = {
            str(f).strip().lower()
            for values in requested_by.values()
            if isinstance(values, list)
            for f in values
        }
        for intent_id, fields in required_by.items():
            if not isinstance(fields, list) or not fields:
                continue
            missing_request = [
                str(field).strip().lower()
                for field in fields
                if str(field).strip().lower() not in requested_all
            ]
            if missing_request:
                reasons.append("INTENT_REQUIRED_INFO_NOT_REQUESTED")
                break

    text = str(body or "").lower()
    if _INTENT_EXECUTION_CLAIM_RE.search(text):
        reasons.append("INTENT_EXECUTION_ASSERTED_WITHOUT_EVIDENCE")
    if unresolved and _INTENT_COMPLETION_CLAIM_RE.search(text):
        reasons.append("INTENT_FALSELY_COMPLETED")
    return reasons


def evaluate_draft_sanity(
    *,
    body: str,
    case_kind: str,
    intent: str = "",
    snapshot: Any = None,
    policy_allows_draft: bool | None = None,
    epistemic_context: Any = None,
    intent_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return {ok, reason_codes, failure_class?} for a customer-facing draft."""
    text = str(body or "").strip()
    reasons: list[str] = []
    if not text:
        reasons.append("empty_body")
    if _PLACEHOLDER_RE.search(text):
        reasons.append("placeholder_or_internal_token")
    if _PROMISE_RE.search(text):
        reasons.append("forbidden_promise")

    service = is_service_case_kind(case_kind)
    if service and _SALES_ASK_RE.search(text):
        # Service cases must not request sales sizing data unless genuinely needed.
        reasons.append("service_draft_asks_sales_fields")
    if service and _UNSUPPORTED_SERVICE_PROMISE_RE.search(text):
        reasons.append("unsupported_service_promise")
    if service and _DIAGNOSIS_CERTAINTY_RE.search(text):
        reasons.append("unsupported_diagnosis_claim")

    known = known_facts_from_snapshot(snapshot) if snapshot is not None else {}
    if known.get("heated_area_m2") is not None and re.search(
        r"\b(prosimy|podaj|uzupełni|brakuje|need|provide)\b.{0,80}"
        r"\b(metr(a[zż]|aż|azu)|powierzchni|m2|m²)\b",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        reasons.append("asks_known_heated_area_m2")
    if known.get("raw_geographic_signal") and re.search(
        r"\b(podaj|prosimy o).{0,40}(miasto|lokalizacj|adres)\b",
        text,
        re.IGNORECASE,
    ):
        reasons.append("asks_known_location")

    if intent == "missing_info" and service and not _SERVICE_CLARIFICATION_RE.search(text):
        reasons.append("service_missing_info_without_service_scope")

    if policy_allows_draft is False:
        reasons.append("policy_disallows_draft")

    # P1.3: structured epistemic guard (UNKNOWN/INFERRED/CONFLICTED must not be
    # asserted as confirmed facts). Defense-in-depth over the deterministic
    # composer; never the primary mechanism.
    if epistemic_context is not None:
        from agent_runtime.epistemic_projection import evaluate_draft_epistemic_sanity

        epistemic = evaluate_draft_epistemic_sanity(
            body=body,
            claim_context=epistemic_context,
        )
        if not epistemic.get("ok"):
            reasons.extend(epistemic.get("reason_codes") or [])

    # P1.4: structural multi-intent coverage (dropped intent / false completion /
    # missing required-information request / execution asserted without
    # evidence). Never silent repair.
    reasons.extend(
        _intent_coverage_issues(
            coverage=intent_coverage,
            body=body,
        )
    )

    if reasons:
        return {
            "ok": False,
            "reason_codes": reasons,
            "failure_class": "DRAFT_SANITY_FAILED",
        }
    return {"ok": True, "reason_codes": []}


__all__ = ["evaluate_draft_sanity"]
