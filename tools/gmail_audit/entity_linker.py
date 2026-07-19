"""Identity-first entity linking for CanonicalSignal -> Case (V2.1 Layer 3).

Resolves which mailbox-memory case a signal belongs to using deterministic keys first,
then bounded fuzzy matching. Emits typed edges and operator-desk events when confidence
is insufficient or conflicts arise.
"""

from __future__ import annotations
from log_config import get_logger

import hashlib
import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import Any

from signal_contract import CanonicalSignal
from case_family_boundary import filter_operational_feed_case_rows

log = get_logger(__name__)

ENTITY_LINK_THRESHOLD = 0.85
FUZZY_MIN_SCORE = 0.55
CONFLICT_SCORE_DELTA = 0.05
POSTGRES_CONNECT_TIMEOUT_SEC = 15

OPERATOR_DESK_CASE_ID = "_operator_desk"
EDGE_REL_BELONGS = "belongs_to"
NODE_SIGNAL = "canonical_signal"
NODE_CASE = "mailbox_case"


@dataclass(slots=True, frozen=True)
class EntityLinkResult:
    """Outcome of identity resolution for one canonical signal."""

    link_status: str
    confidence: float
    phase: str
    case_id: str = ""
    case_key: str = ""
    reasons: tuple[str, ...] = field(default_factory=tuple)
    candidates: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    case_proposal: dict[str, Any] = field(default_factory=dict)
    desk_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_status": self.link_status,
            "confidence": self.confidence,
            "phase": self.phase,
            "case_id": self.case_id,
            "case_key": self.case_key,
            "reasons": list(self.reasons),
            "candidates": [dict(c) for c in self.candidates],
            "case_proposal": dict(self.case_proposal),
            "desk_summary": self.desk_summary,
        }


def _digits_only(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def _normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


_NIP_RE = re.compile(r"\b(\d{10})\b")
_PHONE_RE = re.compile(r"(?:\+?\d[\d\s\-]{7,}\d)")
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


def extract_identity_hints(payload: dict[str, Any], artifacts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collect NIP, contract, phone, email, invoice ids from nested payload/artifacts."""
    hints: dict[str, set[str]] = {
        "nip": set(),
        "contract_number": set(),
        "phone": set(),
        "email": set(),
        "invoice_id": set(),
    }

    def add_bucket(bucket: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                add_bucket(bucket, item)
            return
        text = str(value).strip()
        if not text:
            return
        if bucket == "nip":
            for m in _NIP_RE.finditer(text):
                hints["nip"].add(m.group(1))
        elif bucket == "phone":
            d = _digits_only(text)
            if len(d) >= 9:
                hints["phone"].add(d[-9:] if len(d) > 9 else d)
        elif bucket == "email":
            em = _normalize_email(text)
            if "@" in em:
                hints["email"].add(em)
        elif bucket == "contract_number":
            hints["contract_number"].add(text.upper().replace(" ", ""))
        elif bucket == "invoice_id":
            hints["invoice_id"].add(text.upper().replace(" ", ""))

    def walk(obj: Any, key_path: str = "") -> None:
        if obj is None:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = str(k).lower()
                if lk in {"nip", "company_nip", "tax_id", "vat_id", "vat"}:
                    add_bucket("nip", v)
                    walk(v)
                elif lk in {"contract_number", "contract_id", "contractno", "order_numbers"}:
                    if lk == "order_numbers":
                        add_bucket("contract_number", v)
                    else:
                        add_bucket("contract_number", v)
                    walk(v)
                elif lk in {"phone", "tel", "mobile", "customer_phone"}:
                    add_bucket("phone", v)
                    walk(v)
                elif lk in {"email", "customer_email", "sender_email"}:
                    add_bucket("email", v)
                    walk(v)
                elif lk in {"invoice_numbers", "invoice_number", "invoice_id"}:
                    add_bucket("invoice_id", v)
                    walk(v)
                else:
                    walk(v, f"{key_path}.{k}")
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                walk(item, key_path)
        elif isinstance(obj, str):
            for em in _EMAIL_RE.findall(obj):
                add_bucket("email", em)
            for m in _NIP_RE.finditer(obj):
                add_bucket("nip", m.group(1))
            for chunk in _PHONE_RE.findall(obj):
                add_bucket("phone", chunk)

    walk(payload)
    walk(artifacts or {})

    # Flatten snapshot / intake common paths
    snap = dict(payload.get("snapshot") or {})
    src = dict(snap.get("source_message") or {})
    add_bucket("email", src.get("sender_email") or _extract_email_from_sender(str(src.get("sender") or "")))
    intake = dict(payload.get("intake_result_final") or {})
    refs = dict((intake.get("extracted_data") or {}).get("references") or {})
    add_bucket("invoice_id", refs.get("invoice_numbers"))
    add_bucket("contract_number", refs.get("order_numbers"))
    add_bucket("contract_number", refs.get("transaction_numbers"))

    doc_row = dict(payload.get("document_row") or {})
    meta = dict(doc_row.get("metadata") or {})
    walk(meta)
    for fr in payload.get("fact_rows") or []:
        if isinstance(fr, dict):
            fk = str(fr.get("fact_key") or "").lower()
            nv = str(fr.get("normalized_value") or fr.get("raw_value") or "")
            if fk in {"nip", "company_nip", "tax_id"}:
                add_bucket("nip", nv)
            elif fk in {"contract_number", "order_number"}:
                add_bucket("contract_number", nv)
            elif fk in {"invoice_number", "invoice_id"}:
                add_bucket("invoice_id", nv)
            elif fk in {"phone", "customer_phone"}:
                add_bucket("phone", nv)
            elif fk in {"email", "customer_email"}:
                add_bucket("email", nv)

    return {k: sorted(v) for k, v in hints.items() if v}


def _extract_email_from_sender(sender: str) -> str:
    m = _EMAIL_RE.search(sender or "")
    return m.group(0) if m else ""


def _case_identity_profile(case_row: dict[str, Any], facts: list[dict[str, Any]]) -> dict[str, set[str]]:
    meta = dict(case_row.get("metadata") or {})
    prof: dict[str, set[str]] = {
        "nip": set(),
        "contract_number": set(),
        "phone": set(),
        "email": set(),
        "invoice_id": set(),
        "address": set(),
        "name": set(),
        "city": set(),
    }
    em = _normalize_email(str(case_row.get("customer_email") or ""))
    if em:
        prof["email"].add(em)
    nm = str(case_row.get("customer_name") or "").strip().lower()
    if len(nm) >= 3:
        prof["name"].add(nm)
    subj = str(case_row.get("subject") or "").strip().lower()
    if len(subj) >= 4:
        prof["name"].add(subj)
    for key in ("nip", "company_nip", "tax_id", "vat_id"):
        raw = meta.get(key)
        if not raw:
            continue
        text = str(raw)
        for m in _NIP_RE.finditer(text):
            prof["nip"].add(m.group(1))
        if text.isdigit() and len(text) == 10:
            prof["nip"].add(text)
    for fact in facts:
        fk = str(fact.get("fact_key") or "").lower()
        nv = str(fact.get("normalized_value") or fact.get("raw_value") or "")
        if not nv:
            continue
        if fk in {"nip", "company_nip", "tax_id"}:
            for m in _NIP_RE.finditer(nv):
                prof["nip"].add(m.group(1))
        elif fk in {"contract_number", "order_number"}:
            prof["contract_number"].add(nv.upper().replace(" ", ""))
        elif fk in {"invoice_number", "invoice_id"}:
            prof["invoice_id"].add(nv.upper().replace(" ", ""))
        elif fk in {"phone", "customer_phone"}:
            d = _digits_only(nv)
            if len(d) >= 9:
                prof["phone"].add(d[-9:])
        elif fk in {"email", "customer_email"}:
            prof["email"].update(_normalize_email(nv) for nv in _EMAIL_RE.findall(nv) or [nv])
        elif fk in {"address", "installation_address", "site_address"}:
            prof["address"].add(nv.strip().lower())
        elif fk in {"city", "location_city"}:
            prof["city"].add(nv.strip().lower())
    return prof


def _score_fuzzy(hints: dict[str, Any], profile: dict[str, set[str]]) -> tuple[float, list[str]]:
    """Return best fuzzy score and reasons (0..1)."""
    reasons: list[str] = []
    scores: list[float] = []

    def best_ratio(a: str, pool: set[str]) -> float:
        best = 0.0
        for b in pool:
            if not b:
                continue
            r = SequenceMatcher(None, a, b).ratio()
            if r > best:
                best = r
        return best

    for addr in hints.get("address", []) or []:
        if not isinstance(addr, str):
            continue
        a = addr.strip().lower()
        if len(a) < 6:
            continue
        for paddr in profile.get("address") or []:
            r = SequenceMatcher(None, a, paddr).ratio()
            if r >= 0.82:
                scores.append(r)
                reasons.append("address_fuzzy_match")
                break
        else:
            pool = profile.get("address") or set()
            br = best_ratio(a, pool)
            if br >= 0.75:
                scores.append(br)
                reasons.append("address_fuzzy_match")

    for name in hints.get("client_name", []) or hints.get("name", []) or []:
        if not isinstance(name, str):
            continue
        n = name.strip().lower()
        if len(n) < 3:
            continue
        br = best_ratio(n, profile.get("name") or set())
        if br >= 0.82:
            scores.append(br)
            reasons.append("client_name_fuzzy_match")

    for city in hints.get("city", []) or []:
        c = str(city).strip().lower()
        if len(c) < 3:
            continue
        if c in (profile.get("city") or set()):
            scores.append(0.9)
            reasons.append("city_exact_match")
    if not scores:
        return 0.0, []
    return min(1.0, max(scores)), reasons


def _collect_fuzzy_hints(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, list[str]] = {"address": [], "client_name": [], "city": []}
    intake = dict(payload.get("intake_result_final") or {})
    ld = dict((intake.get("extracted_data") or {}).get("lead_details") or {})
    if ld.get("city"):
        out["city"].append(str(ld["city"]))
    locs = (intake.get("extracted_data") or {}).get("entities", {}).get("locations") or []
    for loc in locs:
        if isinstance(loc, str) and len(loc) >= 4:
            out["address"].append(loc)
    orgs = (intake.get("extracted_data") or {}).get("entities", {}).get("organizations") or []
    for org in orgs:
        if isinstance(org, str) and len(org) >= 3:
            out["client_name"].append(org)
    snap = dict(payload.get("snapshot") or {})
    body = str((snap.get("source_message") or {}).get("body_text") or "")[:4000]
    if len(body) > 20:
        out["address"].append(body[:500])
    return {k: v for k, v in out.items() if v}


class EntityLinker:
    """Identity-first resolver over mailbox-memory cases."""

    def __init__(self, store: Any, *, cases_limit: int = 500) -> None:
        self._store = store
        self._cases_limit = cases_limit

    def find_case(self, signal: CanonicalSignal) -> EntityLinkResult:
        fetcher = getattr(self._store, "fetch_latest_adjudication_link_override", None)
        if callable(fetcher):
            override = fetcher(signal.signal_id)
            if isinstance(override, dict) and str(override.get("override_kind") or "") == "reject_same_case":
                rej = str(override.get("rejected_case_id") or "").strip()
                return EntityLinkResult(
                    link_status="PENDING_ADJUDICATION",
                    confidence=0.0,
                    phase="adjudication",
                    case_id="",
                    case_key="",
                    reasons=("operator_rejected_case_link",),
                    desk_summary=f"Operator odrzucił powiązanie sygnału ze sprawą {rej or '?'}.",
                )

        hints = extract_identity_hints(dict(signal.payload or {}), dict(signal.artifacts or {}))
        fuzzy_hints = _collect_fuzzy_hints(dict(signal.payload or {}))
        merged_hints = {**hints, **{k: v for k, v in fuzzy_hints.items() if v}}

        if not hints and not any(fuzzy_hints.values()):
            return EntityLinkResult(
                link_status="SKIPPED",
                confidence=0.0,
                phase="none",
                reasons=("no_identity_hints",),
            )

        # internal_task rows are not customer-case link targets (Phase 1 boundary).
        cases = filter_operational_feed_case_rows(
            list(self._store.fetch_cases(limit=self._cases_limit) or [])
        )
        deterministic_hits: list[tuple[str, str, str, str]] = []  # case_id, case_key, reason, dimension

        for case_row in cases:
            cid = str(case_row.get("case_id") or "").strip()
            ckey = str(case_row.get("case_key") or "").strip()
            if not cid:
                continue
            facts = self._store.fetch_facts_for_case(cid)
            prof = _case_identity_profile(case_row, facts)

            for nip in hints.get("nip", []) or []:
                if nip in prof["nip"]:
                    deterministic_hits.append((cid, ckey, "nip_exact", "nip"))
            for cn in hints.get("contract_number", []) or []:
                if cn and cn in prof["contract_number"]:
                    deterministic_hits.append((cid, ckey, "contract_exact", "contract_number"))
            for ph in hints.get("phone", []) or []:
                if ph and ph in prof["phone"]:
                    deterministic_hits.append((cid, ckey, "phone_exact", "phone"))
            for em in hints.get("email", []) or []:
                if em and em in prof["email"]:
                    deterministic_hits.append((cid, ckey, "email_exact", "email"))
            for inv in hints.get("invoice_id", []) or []:
                if inv and inv in prof["invoice_id"]:
                    deterministic_hits.append((cid, ckey, "invoice_exact", "invoice_id"))

        uniq_cases = {h[0] for h in deterministic_hits}
        if len(uniq_cases) > 1:
            cands = tuple(
                {
                    "case_id": h[0],
                    "case_key": h[1],
                    "score": 1.0,
                    "source": "entity_match",
                    "reasons": [h[2]],
                    "hard_match_count": 1,
                    "soft_match_count": 0,
                }
                for h in deterministic_hits
            )
            return EntityLinkResult(
                link_status="LINK_CONFLICT",
                confidence=1.0,
                phase="deterministic",
                reasons=("multiple_deterministic_cases",),
                candidates=cands,
                desk_summary="Konflikt tozsamosci: wiele spraw pasuje deterministycznie (NIP/kontrakt/email).",
            )

        if len(uniq_cases) == 1 and deterministic_hits:
            cid = deterministic_hits[0][0]
            ckey = deterministic_hits[0][1]
            dim_reasons = sorted({h[2] for h in deterministic_hits})
            cand = (
                {
                    "case_id": cid,
                    "case_key": ckey,
                    "score": 1.0,
                    "source": "entity_match",
                    "reasons": dim_reasons,
                    "hard_match_count": len(dim_reasons),
                    "soft_match_count": 0,
                },
            )
            return EntityLinkResult(
                link_status="VERIFIED",
                confidence=1.0,
                phase="deterministic",
                case_id=cid,
                case_key=ckey,
                reasons=tuple(dim_reasons),
                candidates=cand,
            )

        # Phase 2 fuzzy
        fuzzy_scores: list[tuple[str, str, float, list[str]]] = []
        for case_row in cases:
            cid = str(case_row.get("case_id") or "").strip()
            ckey = str(case_row.get("case_key") or "").strip()
            if not cid:
                continue
            facts = self._store.fetch_facts_for_case(cid)
            prof = _case_identity_profile(case_row, facts)
            score, reasons = _score_fuzzy(merged_hints, prof)
            if score >= FUZZY_MIN_SCORE:
                fuzzy_scores.append((cid, ckey, score, reasons))

        fuzzy_scores.sort(key=lambda item: item[2], reverse=True)
        if not fuzzy_scores:
            proposal = {
                "kind": "new_case",
                "summary": "Brak dopasowania tozsamosci — proponuj nowa sprawe.",
                "hints": merged_hints,
            }
            return EntityLinkResult(
                link_status="CASE_PROPOSAL",
                confidence=0.0,
                phase="fuzzy",
                reasons=("no_store_match",),
                case_proposal=proposal,
                desk_summary="Nowy klient (brak twardego dopasowania w pamieci skrzynki).",
            )

        top_id, top_key, top_score, top_reasons = fuzzy_scores[0]
        second_score = fuzzy_scores[1][2] if len(fuzzy_scores) > 1 else 0.0

        cands = tuple(
            {
                "case_id": fs[0],
                "case_key": fs[1],
                "score": fs[2],
                "source": "entity_match",
                "reasons": fs[3] or ["fuzzy_match"],
                "hard_match_count": 0,
                "soft_match_count": 1,
            }
            for fs in fuzzy_scores[:5]
        )

        if top_score >= ENTITY_LINK_THRESHOLD and (top_score - second_score) > CONFLICT_SCORE_DELTA:
            return EntityLinkResult(
                link_status="VERIFIED",
                confidence=top_score,
                phase="fuzzy",
                case_id=top_id,
                case_key=top_key,
                reasons=tuple(top_reasons) or ("fuzzy_match",),
                candidates=cands,
            )

        if len(fuzzy_scores) >= 2 and top_score >= 0.75 and (top_score - second_score) <= CONFLICT_SCORE_DELTA:
            return EntityLinkResult(
                link_status="LINK_CONFLICT",
                confidence=top_score,
                phase="fuzzy",
                reasons=("competing_fuzzy_candidates",),
                candidates=cands,
                desk_summary="Konflikt fuzzy: kilka spraw z podobnym dopasowaniem.",
            )

        if top_score < ENTITY_LINK_THRESHOLD:
            return EntityLinkResult(
                link_status="PENDING_ADJUDICATION",
                confidence=top_score,
                phase="fuzzy",
                case_id=top_id,
                case_key=top_key,
                reasons=tuple(top_reasons) or ("below_autonomy_threshold",),
                candidates=cands,
                desk_summary="Niska pewnosc dopasowania — wymaga decyzji operatora.",
            )

        return EntityLinkResult(
            link_status="VERIFIED",
            confidence=top_score,
            phase="fuzzy",
            case_id=top_id,
            case_key=top_key,
            reasons=tuple(top_reasons) or ("fuzzy_match",),
            candidates=cands,
        )


def _ensure_operator_desk_case(store: Any) -> None:
    row = store.fetch_case(OPERATOR_DESK_CASE_ID)
    if row:
        return
    store.upsert_case(
        {
            "case_id": OPERATOR_DESK_CASE_ID,
            "case_key": OPERATOR_DESK_CASE_ID,
            "thread_id": "",
            "case_family": "internal_coordination",
            "mailbox": "system",
            "subject": "Operator desk (identity)",
            "status": "open",
            "customer_name": "",
            "customer_email": "",
            "metadata": {"kind": "system_desk", "role": "entity_link_escalations"},
        }
    )


def _persist_signal_case_edge(
    *,
    store: Any,
    graph_store: Any | None,
    signal: CanonicalSignal,
    case_id: str,
    confidence: float,
) -> None:
    sig_node = f"sig_{signal.signal_id}"
    case_node = f"case_{case_id}"
    if graph_store is not None:
        graph_store.upsert_many(
            nodes=[
                {
                    "node_id": sig_node,
                    "node_type": NODE_SIGNAL,
                    "natural_key": signal.signal_id,
                    "title": signal.signal_kind,
                    "source": "entity_linker",
                    "source_ref": signal.signal_id,
                    "confidence": confidence,
                    "payload": {"signal_kind": signal.signal_kind, "source_kind": signal.source_kind},
                },
                {
                    "node_id": case_node,
                    "node_type": NODE_CASE,
                    "natural_key": case_id,
                    "title": case_id,
                    "source": "entity_linker",
                    "source_ref": case_id,
                    "confidence": 1.0,
                    "payload": {},
                },
            ],
            edges=[
                {
                    "edge_id": f"belongs_{signal.signal_id}_{case_id}",
                    "src_node_id": sig_node,
                    "dst_node_id": case_node,
                    "relation_type": EDGE_REL_BELONGS,
                    "source": "entity_linker",
                    "source_ref": signal.signal_id,
                    "confidence": confidence,
                    "metadata": {"signal_id": signal.signal_id, "case_id": case_id},
                }
            ],
        )
    # Typed edge row in events as durable fallback
    store.append_event(
        {
            "event_id": f"sig_edge_{signal.signal_id}_{case_id}",
            "case_id": case_id,
            "message_id": "",
            "thread_id": "",
            "event_type": "signal_belongs_to_case",
            "occurred_at": signal.observed_at,
            "summary_text": f"Signal {signal.signal_id} -> case {case_id}",
            "payload": {
                "edge": EDGE_REL_BELONGS,
                "signal_id": signal.signal_id,
                "case_id": case_id,
                "confidence": confidence,
                "source": "entity_linker",
            },
            "source_refs": [{"kind": "signal_id", "ref": signal.signal_id}],
        }
    )


def _maybe_record_fingerprint_link(store: Any, signal: CanonicalSignal, case_id: str) -> None:
    obs_id = str((signal.artifacts or {}).get("raw_observation_id") or "").strip()
    if not obs_id:
        return
    row = store.fetch_raw_observation(obs_id)
    fp = str(row.get("source_fingerprint") or "") if row else ""
    if not fp:
        return
    digest = hashlib.sha256(fp.encode("utf-8")).hexdigest()[:24]
    store.append_event(
        {
            "event_id": f"obslink_{digest}",
            "case_id": case_id,
            "message_id": "",
            "thread_id": "",
            "event_type": "observation_source_linked",
            "occurred_at": signal.observed_at,
            "summary_text": "Powiazanie obserwacji z sprawa (fingerprint)",
            "payload": {
                "source_fingerprint": fp,
                "raw_observation_id": obs_id,
                "case_id": case_id,
                "signal_id": signal.signal_id,
            },
            "source_refs": [{"kind": "source_fingerprint", "ref": fp}],
        }
    )


def _emit_operator_desk_event(store: Any, signal: CanonicalSignal, result: EntityLinkResult) -> None:
    _ensure_operator_desk_case(store)
    store.append_event(
        {
            "event_id": f"identity_desk_{signal.signal_id}",
            "case_id": OPERATOR_DESK_CASE_ID,
            "message_id": "",
            "thread_id": "",
            "event_type": "identity_link_operator_desk",
            "occurred_at": signal.observed_at,
            "summary_text": result.desk_summary or f"Identity link: {result.link_status}",
            "payload": {
                "signal_id": signal.signal_id,
                "link_status": result.link_status,
                "confidence": result.confidence,
                "case_id": result.case_id,
                "case_key": result.case_key,
                "candidates": [dict(c) for c in result.candidates],
                "case_proposal": result.case_proposal,
            },
            "source_refs": [{"kind": "signal_id", "ref": signal.signal_id}],
        }
    )


def apply_entity_link(
    signal: CanonicalSignal,
    *,
    store: Any,
    graph_store: Any | None = None,
    run_state: dict[str, Any] | None = None,
) -> tuple[CanonicalSignal, EntityLinkResult]:
    """Resolve identity, merge into signal payload/artifacts, persist edges and desk hooks."""
    linker = EntityLinker(store)
    result = linker.find_case(signal)

    payload = dict(signal.payload or {})
    if result.phase == "adjudication":
        payload.pop("case_id", None)
        payload.pop("entity_link_case_id", None)
        payload.pop("entity_link_case_key", None)
    artifacts = dict(signal.artifacts or {})
    merged = result.to_dict()
    payload["_entity_link"] = merged
    artifacts["entity_link"] = merged

    if result.link_status == "VERIFIED" and result.case_id:
        payload["case_id"] = result.case_id
        payload["entity_link_case_id"] = result.case_id
        payload["entity_link_case_key"] = result.case_key
        _persist_signal_case_edge(store=store, graph_store=graph_store, signal=signal, case_id=result.case_id, confidence=result.confidence)
        _maybe_record_fingerprint_link(store, signal, result.case_id)

    if result.link_status in {"PENDING_ADJUDICATION", "LINK_CONFLICT", "CASE_PROPOSAL"}:
        _emit_operator_desk_event(store, signal, result)

    if run_state is not None:
        run_state.setdefault("summary", {})
        if isinstance(run_state["summary"], dict):
            run_state["summary"].setdefault("entity_link_events", 0)
            try:
                run_state["summary"]["entity_link_events"] = int(run_state["summary"]["entity_link_events"]) + 1
            except (TypeError, ValueError):
                run_state["summary"]["entity_link_events"] = 1

    new_signal = replace(signal, payload=payload, artifacts=artifacts)
    return new_signal, result


# ── Entity Identity Registry (C8 / P1-1) ──────────────────────────────────────


def resolve_entity_by_email(email: str, *, db_url: str | None = None, conn=None) -> str | None:
    """Look up an entity by email in the Correlation Registry (topinstal_identities).

    Returns *identity_id* (str) or *None* if not found.
    Now a facade over CorrelationRegistryStore.
    """
    em = _normalize_email(email)
    if not em:
        return None
    if not db_url:
        return None
    try:
        from correlation_registry.store import build_registry_store
        store = build_registry_store(database_url=db_url)
        row = store.find_identity_by_email(em)
        return str(row["identity_id"]) if row else None
    except Exception:
        log.warning("resolve_entity_by_email: correlation_registry lookup failed for %s", em, exc_info=True)
        return None


def link_entity_identity(
    email: str,
    *,
    canonical_name: str | None = None,
    phone: str | None = None,
    source_system: str = "gmail-agent",
    db_url: str | None = None,
) -> str:
    """Create or return the identity_id for a given email.

    Now a facade over CorrelationRegistryStore (topinstal_identities).
    If the email already exists, updates display_name and returns existing identity_id.
    Otherwise creates a new identity and returns its identity_id.
    """
    em = _normalize_email(email)
    if not em:
        raise ValueError(f"Invalid email: {email!r}")
    if not db_url:
        raise ValueError("db_url is required for identity lookup")
    from correlation_registry.store import build_registry_store
    store = build_registry_store(database_url=db_url)

    # Find existing identity by email
    existing = store.find_identity_by_email(em)
    if existing:
        identity_id = str(existing.get("identity_id") or "")
        if identity_id:
            if canonical_name:
                store.update_identity_display_name(identity_id, str(canonical_name))
            return identity_id

    # Create new identity
    new_id = store.create_identity(email=em, display_name=str(canonical_name or ""))
    return new_id


def backfill_entity_registry(*, db_url: str) -> int:
    """Backfill: for each distinct email in mailbox_memory_cases, ensure a
    correlation_registry identity exists (topinstal_identities).

    This replaces the old entity_registry backfill. Entity_registry table
    is no longer used — all identity data lives in correlation_registry.
    Returns the number of emails processed.
    """
    import psycopg  # type: ignore[import-not-found]

    conn = psycopg.connect(str(db_url), connect_timeout=POSTGRES_CONNECT_TIMEOUT_SEC)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT customer_email FROM mailbox_memory_cases "
                "WHERE customer_email IS NOT NULL AND customer_email <> ''"
            )
            rows = cur.fetchall()
        processed = 0
        for (email,) in rows:
            try:
                link_entity_identity(email, db_url=db_url)
                processed += 1
            except Exception:
                log.warning("backfill_entity_registry: skipped %s", email)
        conn.commit()
        log.info("backfill_entity_registry: processed %d distinct emails into correlation_registry", processed)
        return processed
    finally:
        conn.close()


__all__ = [
    "ENTITY_LINK_THRESHOLD",
    "EntityLinker",
    "EntityLinkResult",
    "apply_entity_link",
    "extract_identity_hints",
    "resolve_entity_by_email",
    "link_entity_identity",
    "backfill_entity_registry",
]
