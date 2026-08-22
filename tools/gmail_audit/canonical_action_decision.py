"""CanonicalActionDecision (CAD) — deterministic semantic authority boundary.

P0 of ``AI-OS INTELLIGENCE SPINE — CONTRACT + FIRST ENFORCED SLICE``.
Contract owner: ``knowledge/docs/AI_OS_INTELLIGENCE_SPINE_CONTRACT.md``
(§CanonicalActionDecision).

Core invariant: once a CAD is created for goal/action_type/target/channel, no
further layer may change those four fields. Downstream may execute, restrict,
block, or request an explicit revision (``DecisionRevisionRequest``), but must
not reinterpret the decision.

Lifecycle split:

```text
BusinessDecisionProposal
  -> CanonicalizationFailure        # BEFORE CAD exists; no decision_id yet
  -> NEEDS_REVIEW                   # workflow state, never a new action_type

CanonicalActionDecision (FROZEN)
  -> DecisionRevisionRequest        # AFTER CAD exists; carries decision_id
  -> decision owner -> new CAD
```

This module is deliberately narrow: the first enforced slice is
``ask_for_missing_data / customer / mail``. Other action classes are added
only after the pattern is proven on this slice.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

CANONICAL_ACTION_DECISION_SCHEMA_VERSION = "canonical_action_decision.v1"
BUSINESS_DECISION_PROPOSAL_SCHEMA_VERSION = "business_decision_proposal.v1"
CANONICALIZATION_FAILURE_SCHEMA_VERSION = "canonicalization_failure.v1"
DECISION_REVISION_REQUEST_SCHEMA_VERSION = "decision_revision_request.v1"
DECISION_REVISION_EVENT_SCHEMA_VERSION = "decision_revision_event.v1"

# Canonical vocabularies. The first slice is intentionally narrow; each new
# action class is added with its own legal channel/target rules and tests.
CANONICAL_GOALS = ("obtain_missing_service_information",)
CANONICAL_ACTION_TYPES = ("ask_for_missing_data",)
CANONICAL_TARGETS = ("customer", "operator", "supplier", "internal", "none")
CANONICAL_CHANNELS = ("mail", "phone", "internal", "none")

# Legal channels per canonical action_type (mirrors the case_intelligence
# ACTION_CHANNEL table for the slice; no channel may be invented downstream).
ACTION_TYPE_CHANNELS: dict[str, tuple[str, ...]] = {
    "ask_for_missing_data": ("mail",),
}

# Default target when a proposal omits it (the slice has exactly one mapping).
ACTION_TYPE_DEFAULT_TARGET: dict[str, str] = {
    "ask_for_missing_data": "customer",
}

# CanonicalizationFailure reason codes (deterministic, machine-readable).
FAILURE_REASON_CODES = (
    "proposal_incomplete",
    "action_type_not_in_contract",
    "goal_not_in_contract",
    "target_not_in_contract",
    "channel_not_in_contract",
    "channel_illegal_for_action_type",
    "required_information_empty",
    "required_information_not_in_state",
    "missing_information_unavailable",
    "conflicted_fact_as_certainty",
)

# DecisionRevisionRequest reason codes (P1.1: enum, no ad-hoc expansion).
REVISION_REASON_CODES = (
    "NEW_CONFLICTING_EVIDENCE",
    "FAILED_PRECONDITION",
    "CANONICAL_FACT_CHANGED",
    "STALE_SITUATION",
    "TOOL_CAPABILITY_MISSING",
    "POLICY_REEVALUATION_REQUIRED",
    "IMPOSSIBLE_PRECONDITION",
    "OUT_OF_SCOPE_REQUEST",
)

# DecisionRevisionRequest lifecycle statuses.
REVISION_REQUEST_STATUSES = ("PENDING", "ACCEPTED", "REJECTED", "SUPERSEDED")

# CAD revision status: exactly one CURRENT revision per decision lineage.
REVISION_STATUSES = ("CURRENT", "SUPERSEDED")

# Deterministic observability codes for revision lifecycle (failure taxonomy).
REVISION_OBSERVABILITY_CODES = (
    "DECISION_REVISION_REQUIRED",
    "DECISION_REVISION_ACCEPTED",
    "DECISION_REVISION_REJECTED",
    "STALE_DECISION_REVISION",
    "STALE_REVISION_REQUEST",
    "DUPLICATE_REVISION_REQUEST",
    "SUPERSEDED_DECISION_ARTIFACT",
)


class DecisionRevisionError(RuntimeError):
    """Fail-closed revision lifecycle violation (e.g. one-current invariant)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, round(number, 4)))


def _as_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _string(value: Any, default: str = "") -> str:
    return str(value or "").strip() or default


def _canonical_payload(
    *,
    schema_version: str,
    case_id: str,
    situation_version: str,
    goal: str,
    action_type: str,
    target: str,
    channel: str,
    required_information: list[str],
) -> str:
    """Canonical JSON for the semantic hash.

    Deliberately excludes created_at, rationale, confidence and presentation
    fields: identity of the semantic signature must not depend on them.
    required_information is sorted so ordering does not change the hash.
    """
    payload = {
        "schema_version": schema_version,
        "case_id": _string(case_id),
        "situation_version": _string(situation_version),
        "goal": _string(goal),
        "action_type": _string(action_type),
        "target": _string(target),
        "channel": _string(channel),
        "required_information": sorted(_as_list(required_information)),
        "semantic_status": "FROZEN",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def semantic_hash_of(
    *,
    schema_version: str = CANONICAL_ACTION_DECISION_SCHEMA_VERSION,
    case_id: str = "",
    situation_version: str = "",
    goal: str = "",
    action_type: str = "",
    target: str = "",
    channel: str = "",
    required_information: list[str] | None = None,
) -> str:
    """SHA256 of the canonical semantic payload (Semantic Conservation basis)."""
    canonical = _canonical_payload(
        schema_version=schema_version,
        case_id=case_id,
        situation_version=situation_version,
        goal=goal,
        action_type=action_type,
        target=target,
        channel=channel,
        required_information=list(required_information or []),
    )
    return "sh_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _proposal_id() -> str:
    return "bprop_" + uuid.uuid4().hex[:22]


def _decision_id() -> str:
    return "dec_" + uuid.uuid4().hex[:22]


def _request_id() -> str:
    return "revreq_" + uuid.uuid4().hex[:22]


def decision_version_id_of(decision_id: str, revision: int) -> str:
    """Unique concrete decision version identity, e.g. ``dec_abc:r2``."""
    return f"{_string(decision_id)}:r{max(1, int(revision or 1))}"


def _risk_class_from_business(business_result: dict[str, Any]) -> str:
    urgency = str(business_result.get("urgency") or "").strip().lower()
    if urgency in {"high", "critical"}:
        return "high"
    if urgency in {"medium"}:
        return "medium"
    return "low"


def build_business_decision_proposal(
    business_reasoning_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Derive a typed BusinessDecisionProposal from the existing BR result.

    P0 supports exactly one proposal class: ``collect_data`` ->
    ``ask_for_missing_data / customer / mail``. Other BR actions return None
    (the legacy path keeps working unchanged; there is no proposal to
    canonicalize and therefore no CanonicalizationFailure).

    ``BusinessReasoningResult`` itself is NOT extended (consumer inventory:
    ~80 files touch the recommendation surface).
    """
    br = business_reasoning_result if isinstance(business_reasoning_result, dict) else {}
    action = _string(br.get("recommended_next_action"))
    if action != "collect_data":
        return None

    missing = _as_list(br.get("missing_information"))
    confidence = 0.0
    conf = br.get("confidence")
    if isinstance(conf, dict):
        confidence = _as_float(conf.get("action_confidence"))
    elif isinstance(conf, (int, float)):
        confidence = _as_float(conf)

    return {
        "schema_version": BUSINESS_DECISION_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": _proposal_id(),
        "goal": "obtain_missing_service_information",
        "action_type": "ask_for_missing_data",
        "target": ACTION_TYPE_DEFAULT_TARGET["ask_for_missing_data"],
        "channel": "mail",
        "required_information": missing,
        "confidence": confidence,
        "reason": _string(br.get("recommended_action_reason")),
        "risk_class": _risk_class_from_business(br),
    }


def _state_missing_information(
    *,
    situation_understanding: dict[str, Any] | None,
    case_context_pack: dict[str, Any] | None,
    intake_result: dict[str, Any] | None,
) -> list[str]:
    """Collect the state's missing-information surface.

    SituationUnderstanding is state, not a decision owner: this function only
    proves that the proposal's required_information is supported by the state.
    """
    state: list[str] = []
    su = situation_understanding if isinstance(situation_understanding, dict) else {}
    state.extend(_as_list(su.get("missing_information")))
    state.extend(_as_list(su.get("missing_critical_fields")))
    state.extend(_as_list(su.get("required_information")))

    pack = case_context_pack if isinstance(case_context_pack, dict) else {}
    for row in pack.get("completeness_gaps") or []:
        if isinstance(row, dict):
            label = _string(row.get("label"), _string(row.get("field_name"), _string(row.get("fact_key"))))
            if label:
                state.append(label)

    intake = intake_result if isinstance(intake_result, dict) else {}
    state.extend(_as_list(intake.get("missing_information")))
    return _dedupe(state)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def _conflicted_fact_keys(case_context_pack: dict[str, Any] | None) -> set[str]:
    """Keys whose live value set is conflicted/uncertain (decision_usable=False)."""
    pack = case_context_pack if isinstance(case_context_pack, dict) else {}
    keys: set[str] = set()
    for row in pack.get("conflicting_facts") or []:
        if not isinstance(row, dict):
            continue
        key = _string(row.get("fact_key"), _string(row.get("key")))
        if not key:
            continue
        decision_usable = row.get("decision_usable")
        trust_state = _string(row.get("trust_state"))
        if decision_usable is False or trust_state == "conflicted":
            keys.add(key.lower())
    return keys


def _proposal_failure(proposal: dict[str, Any], reason_codes: list[str], failed_precondition: str = "") -> dict[str, Any]:
    return {
        "schema_version": CANONICALIZATION_FAILURE_SCHEMA_VERSION,
        "proposal_id": _string(proposal.get("proposal_id")),
        "reason_codes": list(reason_codes),
        "failed_precondition": failed_precondition or (reason_codes[0] if reason_codes else ""),
        "occurred_at": _utc(),
        "decision_state": "NO_CANONICAL_DECISION",
    }


def canonicalize(
    *,
    proposal: dict[str, Any] | None,
    situation_understanding: dict[str, Any] | None = None,
    case_context_pack: dict[str, Any] | None = None,
    intake_result: dict[str, Any] | None = None,
    case_id: str = "",
    situation_version: str = "",
    decision_id: str = "",
    revision: int = 1,
) -> dict[str, Any]:
    """Deterministic canonicalization boundary.

    Returns either a frozen CanonicalActionDecision or a
    CanonicalizationFailure. A failure NEVER becomes a different business
    action (no escalate_review fallback) — the workflow outcome is
    NEEDS_REVIEW via ``canonicalization_failure_review_state``.
    """
    prop = proposal if isinstance(proposal, dict) else None
    if prop is None:
        return _proposal_failure(
            {"proposal_id": ""},
            ["proposal_incomplete"],
            failed_precondition="business_decision_proposal",
        )

    reason_codes: list[str] = []
    goal = _string(prop.get("goal"))
    action_type = _string(prop.get("action_type"))
    target = _string(prop.get("target"))
    channel = _string(prop.get("channel"))
    required = _as_list(prop.get("required_information"))

    if goal not in CANONICAL_GOALS:
        reason_codes.append("goal_not_in_contract")
    if action_type not in CANONICAL_ACTION_TYPES:
        reason_codes.append("action_type_not_in_contract")
    if target not in CANONICAL_TARGETS:
        reason_codes.append("target_not_in_contract")
    if channel not in CANONICAL_CHANNELS:
        reason_codes.append("channel_not_in_contract")
    if action_type in ACTION_TYPE_CHANNELS and channel not in ACTION_TYPE_CHANNELS[action_type]:
        reason_codes.append("channel_illegal_for_action_type")
    if not required:
        reason_codes.append("required_information_empty")

    if not reason_codes:
        state_missing = _state_missing_information(
            situation_understanding=situation_understanding,
            case_context_pack=case_context_pack,
            intake_result=intake_result,
        )
        if not state_missing:
            reason_codes.append("missing_information_unavailable")
        else:
            state_lower = {str(item).lower() for item in state_missing}
            unsupported = [
                item for item in required if str(item).lower() not in state_lower
            ]
            if unsupported:
                reason_codes.append("required_information_not_in_state")

        conflicted = _conflicted_fact_keys(case_context_pack)
        if conflicted:
            used_conflicted = [
                item for item in required if str(item).lower() in conflicted
            ]
            if used_conflicted:
                reason_codes.append("conflicted_fact_as_certainty")

    if reason_codes:
        return _proposal_failure(prop, reason_codes)

    cid = _string(case_id)
    sv = _string(situation_version)
    dec_id = _string(decision_id) or _decision_id()
    rev = max(1, int(revision or 1))
    semantic_hash = semantic_hash_of(
        case_id=cid,
        situation_version=sv,
        goal=goal,
        action_type=action_type,
        target=target,
        channel=channel,
        required_information=required,
    )
    return {
        "schema_version": CANONICAL_ACTION_DECISION_SCHEMA_VERSION,
        "decision_id": dec_id,
        "revision": rev,
        "decision_version_id": decision_version_id_of(dec_id, rev),
        "case_id": cid,
        "situation_version": sv,
        "goal": goal,
        "action_type": action_type,
        "target": target,
        "channel": channel,
        "required_information": required,
        "confidence": _as_float(prop.get("confidence")),
        "risk_class": _string(prop.get("risk_class"), "low"),
        "semantic_hash": semantic_hash,
        "semantic_status": "FROZEN",
        # P1.1: exactly one CURRENT revision per decision lineage.
        "revision_status": "CURRENT",
        "proposal_id": _string(prop.get("proposal_id")),
        "provenance": {
            "proposal_id": _string(prop.get("proposal_id")),
            "situation_version": sv,
        },
        "created_at": _utc(),
    }


def canonical_decision_code(canonical_decision: dict[str, Any]) -> str:
    """Canonical code string for downstream normalization (A17 unification)."""
    if not isinstance(canonical_decision, dict):
        return ""
    return _string(canonical_decision.get("action_type"))


def semantic_signature_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Semantic Conservation check: same canonical semantic payload."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return _string(left.get("semantic_hash")) == _string(right.get("semantic_hash"))


def canonicalization_failure_review_state(failure: dict[str, Any]) -> dict[str, Any]:
    """Map a CanonicalizationFailure to a workflow state.

    NEEDS_REVIEW is a workflow state, never a new action_type and never an
    escalate_review substitution.
    """
    failure = failure if isinstance(failure, dict) else {}
    return {
        "workflow_state": "NEEDS_REVIEW",
        "decision_state": _string(failure.get("decision_state"), "NO_CANONICAL_DECISION"),
        "reason_codes": _as_list(failure.get("reason_codes")),
        "failed_precondition": _string(failure.get("failed_precondition")),
        "operational_status": "pending_operator_review",
        "proposal_id": _string(failure.get("proposal_id")),
    }


def build_decision_revision_request(
    *,
    decision_id: str,
    revision: int = 1,
    reason_code: str = "NEW_CONFLICTING_EVIDENCE",
    failed_precondition: str = "",
    source_layer: str = "",
    source_event_id: str = "",
    evidence_refs: list[dict[str, Any]] | None = None,
    request_id: str = "",
    requested_at: str = "",
    status: str = "PENDING",
) -> dict[str, Any]:
    """Build a DecisionRevisionRequest (P1.1 public contract).

    Emitted after a CAD exists when downstream discovers new evidence, a
    conflict, or an impossible precondition. Downstream MAY request revision;
    only the canonical decision boundary may create a new CAD revision.
    """
    if reason_code not in REVISION_REASON_CODES:
        reason_code = "NEW_CONFLICTING_EVIDENCE"
    if status not in REVISION_REQUEST_STATUSES:
        status = "PENDING"
    evidence = (
        [dict(item) for item in evidence_refs if isinstance(item, dict)]
        if isinstance(evidence_refs, list)
        else []
    )
    rev = max(1, int(revision or 1))
    dec_id = _string(decision_id)
    return {
        "schema_version": DECISION_REVISION_REQUEST_SCHEMA_VERSION,
        "request_id": _string(request_id) or _request_id(),
        "decision_id": dec_id,
        "current_revision": rev,
        # Backward-compatible alias for P0 consumers (kept in sync).
        "revision": rev,
        "current_decision_version_id": decision_version_id_of(dec_id, rev),
        "reason_code": reason_code,
        "failed_precondition": _string(failed_precondition),
        "source_layer": _string(source_layer),
        "source_event_id": _string(source_event_id),
        "evidence_refs": evidence,
        "requested_at": _string(requested_at) or _utc(),
        "status": status,
    }


class DecisionRevisionLedger:
    """Canonical decision-lineage state (P1.1).

    Owns the revision state machine: exactly one CURRENT (execution-eligible)
    revision per decision_id, expected-revision concurrency guard, duplicate
    request detection and an append-only audit trail.

    P1.1P: when a durable ``store`` (MailboxMemoryStore) is provided this
    ledger is a projection/cache over durable state, not the Source of Truth.
    Every mutation writes through to the store first; ``rebuild()`` restores
    the projection after a process restart and fails closed on any
    one-current-revision invariant violation. ``event_sink`` remains an
    optional observability hook (existing event memory).
    """

    def __init__(self, *, event_sink: Any | None = None, store: Any | None = None) -> None:
        self._lock = threading.RLock()
        self._lineages: dict[str, list[dict[str, Any]]] = {}
        self._requests: list[dict[str, Any]] = []
        self._audit: list[dict[str, Any]] = []
        self._event_sink = event_sink
        self._store = store

    @classmethod
    def from_store(cls, store: Any, *, event_sink: Any | None = None) -> "DecisionRevisionLedger":
        """Build a store-backed ledger and rebuild its projection from durable state."""
        ledger = cls(store=store, event_sink=event_sink)
        ledger.rebuild()
        return ledger

    def rebuild(self) -> None:
        """Rebuild the in-memory projection from the durable store.

        Fail closed when a durable lineage has zero or multiple CURRENT
        revisions (never pick "latest by timestamp"). Ordering is by the
        monotonic revision integer only; timestamps are observability.
        """
        if self._store is None:
            return
        with self._lock:
            self._lineages = {}
            self._requests = []
            self._audit = []
            for decision_id in self._store.list_decision_lineage_ids():
                revisions = [
                    dict(item) for item in self._store.fetch_decision_revisions(decision_id)
                ]
                revisions.sort(key=lambda item: int(item.get("revision") or 0))
                current_count = sum(
                    1
                    for item in revisions
                    if _string(item.get("revision_status")) == "CURRENT"
                )
                if revisions and current_count != 1:
                    raise DecisionRevisionError(
                        "rebuild_one_current_violation",
                        f"durable lineage {decision_id} has {current_count} CURRENT revisions",
                    )
                self._lineages[decision_id] = revisions
                requests = [
                    dict(item)
                    for item in self._store.fetch_decision_revision_requests(decision_id)
                ]
                requests.sort(key=lambda item: _string(item.get("requested_at")))
                self._requests.extend(requests)
                self._audit.extend(self._rebuild_audit(decision_id, revisions, requests))

    def _rebuild_audit(
        self,
        decision_id: str,
        revisions: list[dict[str, Any]],
        requests: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Reconstruct the audit trail from durable lineage/request state."""
        entries: list[dict[str, Any]] = []
        by_supersedes: dict[str, dict[str, Any]] = {}
        for rev in revisions:
            src = _string(rev.get("supersedes_version_id"))
            if src:
                by_supersedes[src] = rev
        for request in requests:
            rid = _string(request.get("request_id"))
            status = _string(request.get("status"))
            reason = _string(request.get("reason_code"))
            created = _string(request.get("requested_at"), _string(request.get("created_at")))
            if status == "ACCEPTED":
                old_version = _string(request.get("current_decision_version_id"))
                new_rev = by_supersedes.get(old_version)
                entries.append(
                    {
                        "decision_id": decision_id,
                        "request_id": rid,
                        "old_version_id": old_version,
                        "new_version_id": _string(new_rev.get("decision_version_id")) if new_rev else "",
                        "reason_code": reason,
                        "outcome": "ACCEPTED",
                        "created_at": created,
                    }
                )
            elif status == "REJECTED":
                entries.append(
                    {
                        "decision_id": decision_id,
                        "request_id": rid,
                        "reason_code": reason,
                        "outcome": "REJECTED",
                        "reject_reason": _string(request.get("reject_reason")),
                        "created_at": created,
                    }
                )
        return entries

    # -- lineage -----------------------------------------------------------

    def register_cad(self, cad: dict[str, Any]) -> dict[str, Any]:
        """Register a CAD revision in its lineage (idempotent for same version)."""
        cad = cad if isinstance(cad, dict) else {}
        decision_id = _string(cad.get("decision_id"))
        version_id = _string(cad.get("decision_version_id"))
        if not decision_id or not version_id:
            raise DecisionRevisionError("invalid_cad_identity", "missing decision_id/version")
        with self._lock:
            lineage = self._lineages.setdefault(decision_id, [])
            current = self._current_locked(decision_id)
            if current is not None and current.get("decision_version_id") == version_id:
                return current
            if current is not None:
                raise DecisionRevisionError(
                    "one_current_revision_violation",
                    f"{decision_id} already has CURRENT {current.get('decision_version_id')}",
                )
            if self._store is not None:
                self._store.append_decision_revision(dict(cad))
            lineage.append(dict(cad))
            self._emit("decision_registered", cad)
            return dict(cad)

    def _current_locked(self, decision_id: str) -> dict[str, Any] | None:
        for cad in reversed(self._lineages.get(decision_id, [])):
            if _string(cad.get("revision_status")) == "CURRENT":
                return cad
        return None

    def current_cad(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            current = self._current_locked(_string(decision_id))
            return dict(current) if current is not None else None

    def current_revision(self, decision_id: str) -> int:
        current = self.current_cad(_string(decision_id))
        return int((current or {}).get("revision") or 0)

    def expected_revision(self, decision_id: str) -> int:
        return self.current_revision(_string(decision_id)) + 1

    def is_current(self, cad: dict[str, Any]) -> bool:
        cad = cad if isinstance(cad, dict) else {}
        decision_id = _string(cad.get("decision_id"))
        version_id = _string(cad.get("decision_version_id"))
        if not decision_id or not version_id:
            return False
        current = self.current_cad(decision_id)
        return current is not None and _string(current.get("decision_version_id")) == version_id

    def revisions(self, decision_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._lineages.get(_string(decision_id), [])]

    # -- requests ----------------------------------------------------------

    def record_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Record a request with concurrency guards.

        Returns ``{request, status}`` where status is PENDING,
        STALE_REVISION_REQUEST (expected revision mismatch) or
        DUPLICATE_REVISION_REQUEST (identical pending request already exists).
        """
        request = dict(request)
        decision_id = _string(request.get("decision_id"))
        expected = self.current_revision(decision_id)
        with self._lock:
            for existing in self._requests:
                if _string(existing.get("request_id")) == _string(request.get("request_id")):
                    return {"request": existing, "status": "DUPLICATE_REVISION_REQUEST"}
                if (
                    _string(existing.get("decision_id")) == decision_id
                    and int(existing.get("current_revision") or 0) == int(request.get("current_revision") or 0)
                    and _string(existing.get("reason_code")) == _string(request.get("reason_code"))
                    and _string(existing.get("status")) == "PENDING"
                ):
                    return {"request": existing, "status": "DUPLICATE_REVISION_REQUEST"}
            if int(request.get("current_revision") or 0) != expected:
                stale = dict(request)
                stale["status"] = "REJECTED"
                stale["reject_reason"] = "STALE_REVISION_REQUEST"
                if self._store is not None:
                    self._store.append_decision_revision_request(dict(stale))
                self._requests.append(stale)
                self._audit.append(
                    {
                        "decision_id": decision_id,
                        "request_id": _string(stale.get("request_id")),
                        "expected_revision": expected,
                        "actual_revision": int(request.get("current_revision") or 0),
                        "outcome": "REJECTED",
                        "reason_code": "STALE_REVISION_REQUEST",
                        "created_at": _utc(),
                    }
                )
                return {"request": stale, "status": "STALE_REVISION_REQUEST"}
            if self._store is not None:
                self._store.append_decision_revision_request(dict(request))
            self._requests.append(request)
            self._emit("decision_revision_requested", request)
            return {"request": request, "status": "PENDING"}

    def request_status(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            for request in self._requests:
                if _string(request.get("request_id")) == _string(request_id):
                    return dict(request)
        return None

    # -- accept / reject ---------------------------------------------------

    def accept_revision(
        self,
        *,
        old_cad: dict[str, Any],
        new_cad: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        decision_id = _string(old_cad.get("decision_id"))
        if _string(new_cad.get("decision_id")) != decision_id:
            raise DecisionRevisionError("revision_decision_id_mismatch")
        if int(new_cad.get("revision") or 0) != int(old_cad.get("revision") or 0) + 1:
            raise DecisionRevisionError("revision_not_monotonic")
        if not self.is_current(old_cad):
            raise DecisionRevisionError("stale_revision_request", "old_cad is not current")
        with self._lock:
            current = self._current_locked(decision_id)
            if (
                current is None
                or _string(current.get("decision_version_id"))
                != _string(old_cad.get("decision_version_id"))
            ):
                raise DecisionRevisionError("stale_revision_request", "old_cad is not current")
            old_version = _string(old_cad.get("decision_version_id"))
            new_version = _string(new_cad.get("decision_version_id"))
            superseded_old = {
                **dict(old_cad),
                "revision_status": "SUPERSEDED",
                "superseded_by_version_id": new_version,
            }
            new_stamped = {
                **dict(new_cad),
                "revision_status": "CURRENT",
                "supersedes_version_id": old_version,
            }
            if self._store is not None:
                # Durable transition first (atomic): old -> SUPERSEDED,
                # new -> CURRENT, request -> ACCEPTED.
                self._store.accept_decision_revision_transition(
                    old_cad=old_cad,
                    new_cad=new_cad,
                    request=request,
                )
            lineage = self._lineages.get(decision_id, [])
            for idx, item in enumerate(lineage):
                if _string(item.get("decision_version_id")) == old_version:
                    lineage[idx] = dict(superseded_old)
            lineage.append(dict(new_stamped))
            request = dict(request)
            request["status"] = "ACCEPTED"
            for idx, item in enumerate(self._requests):
                if _string(item.get("request_id")) == _string(request.get("request_id")):
                    self._requests[idx] = request
            self._audit.append(
                {
                    "decision_id": decision_id,
                    "request_id": _string(request.get("request_id")),
                    "old_version_id": old_version,
                    "new_version_id": new_version,
                    "reason_code": _string(request.get("reason_code")),
                    "outcome": "ACCEPTED",
                    "created_at": _utc(),
                }
            )
            self._emit("decision_revision_accepted", new_stamped)
            return {
                "outcome": "ACCEPTED",
                "old_cad": dict(superseded_old),
                "new_cad": dict(new_stamped),
                "request": request,
            }

    def reject_request(
        self,
        *,
        request: dict[str, Any],
        reason: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            request = dict(request)
            request["status"] = "REJECTED"
            request["reject_reason"] = _string(reason) or "REVISION_NOT_JUSTIFIED"
            if self._store is not None:
                self._store.append_decision_revision_request(dict(request))
            for idx, item in enumerate(self._requests):
                if _string(item.get("request_id")) == _string(request.get("request_id")):
                    self._requests[idx] = request
            self._audit.append(
                {
                    "decision_id": _string(request.get("decision_id")),
                    "request_id": _string(request.get("request_id")),
                    "reason_code": _string(request.get("reason_code")),
                    "outcome": "REJECTED",
                    "reject_reason": request["reject_reason"],
                    "created_at": _utc(),
                }
            )
            self._emit("decision_revision_rejected", request)
            current = self._current_locked(_string(request.get("decision_id")))
            return {
                "outcome": "REJECTED",
                "current_cad": dict(current) if current is not None else None,
                "request": request,
            }

    def audit_trail(self, decision_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(item)
                for item in self._audit
                if _string(item.get("decision_id")) == _string(decision_id)
            ]

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(
                {
                    "schema_version": DECISION_REVISION_EVENT_SCHEMA_VERSION,
                    "event_type": event_type,
                    "decision_id": _string(payload.get("decision_id")),
                    "decision_version_id": _string(payload.get("decision_version_id")),
                    "revision": int(payload.get("revision") or 0),
                    "created_at": _utc(),
                }
            )
        except Exception:
            # Observability must never break the canonical lifecycle.
            return


def request_decision_revision(
    *,
    decision_id: str,
    current_revision: int,
    reason_code: str = "NEW_CONFLICTING_EVIDENCE",
    failed_precondition: str = "",
    source_layer: str = "",
    source_event_id: str = "",
    evidence_refs: list[dict[str, Any]] | None = None,
    ledger: DecisionRevisionLedger | None = None,
) -> dict[str, Any]:
    """Canonical emission path for a revision request (downstream's only right)."""
    request = build_decision_revision_request(
        decision_id=decision_id,
        revision=current_revision,
        reason_code=reason_code,
        failed_precondition=failed_precondition,
        source_layer=source_layer,
        source_event_id=source_event_id,
        evidence_refs=evidence_refs,
    )
    ledger = ledger if ledger is not None else _DEFAULT_LEDGER
    recorded = ledger.record_request(request)
    return {
        "request": recorded["request"],
        "status": recorded["status"],
        "request_id": _string(recorded["request"].get("request_id")),
    }


def evaluate_decision_revision(
    *,
    request: dict[str, Any],
    current_cad: dict[str, Any],
    business_reasoning_result: dict[str, Any] | None,
    situation_understanding: dict[str, Any] | None = None,
    case_context_pack: dict[str, Any] | None = None,
    intake_result: dict[str, Any] | None = None,
    ledger: DecisionRevisionLedger | None = None,
) -> dict[str, Any]:
    """Canonical re-evaluation (the ONLY way a new CAD revision is created).

    ACCEPT  -> new CAD revision FROZEN/CURRENT, old CAD SUPERSEDED.
    REJECT  -> current CAD unchanged; request REJECTED.
    The revision request is a trigger + evidence pointer, never an instruction
    that directly sets canonical semantics.
    """
    ledger = ledger if ledger is not None else _DEFAULT_LEDGER
    request = request if isinstance(request, dict) else {}
    if _string(request.get("status")) != "PENDING":
        return {
            "outcome": "REJECTED",
            "reason_codes": ["DECISION_REVISION_REJECTED"],
            "request": request,
            "current_cad": current_cad,
            "new_cad": None,
        }
    current = current_cad if isinstance(current_cad, dict) else {}
    decision_id = _string(request.get("decision_id"))
    if _string(current.get("decision_id")) != decision_id or not ledger.is_current(current):
        return {
            "outcome": "REJECTED",
            "reason_codes": ["STALE_REVISION_REQUEST"],
            "request": request,
            "current_cad": current,
            "new_cad": None,
        }

    proposal = build_business_decision_proposal(business_reasoning_result)
    if proposal is None:
        ledger.reject_request(request=request, reason="NO_BOUNDED_CANONICAL_DECISION")
        return {
            "outcome": "REJECTED",
            "reason_codes": ["DECISION_REVISION_REJECTED", "NO_BOUNDED_CANONICAL_DECISION"],
            "review_state": "NEEDS_REVIEW",
            "request": ledger.request_status(_string(request.get("request_id"))) or request,
            "current_cad": current,
            "new_cad": None,
        }

    new_revision = int(current.get("revision") or 0) + 1
    outcome = canonicalize(
        proposal=proposal,
        situation_understanding=situation_understanding,
        case_context_pack=case_context_pack,
        intake_result=intake_result,
        case_id=_string(current.get("case_id")),
        situation_version=_string(current.get("situation_version")),
        decision_id=decision_id,
        revision=new_revision,
    )
    if outcome.get("semantic_status") != "FROZEN":
        ledger.reject_request(
            request=request,
            reason="CANONICALIZATION_FAILED:" + ",".join(_as_list(outcome.get("reason_codes"))),
        )
        return {
            "outcome": "REJECTED",
            "reason_codes": ["DECISION_REVISION_REJECTED"] + _as_list(outcome.get("reason_codes")),
            "request": ledger.request_status(_string(request.get("request_id"))) or request,
            "current_cad": current,
            "new_cad": None,
        }

    accepted = ledger.accept_revision(old_cad=current, new_cad=outcome, request=request)
    return {
        "outcome": "ACCEPTED",
        "reason_codes": ["DECISION_REVISION_ACCEPTED"],
        "request": accepted["request"],
        "old_cad": accepted["old_cad"],
        "new_cad": accepted["new_cad"],
    }


def artifact_version_matches(artifact: dict[str, Any], current_cad: dict[str, Any]) -> bool:
    """Stale-artifact guard: artifact must bind the current decision version."""
    artifact = artifact if isinstance(artifact, dict) else {}
    current_cad = current_cad if isinstance(current_cad, dict) else {}
    artifact_version = _string(artifact.get("decision_version_id"))
    current_version = _string(current_cad.get("decision_version_id"))
    return bool(artifact_version and current_version and artifact_version == current_version)


def stale_artifact_reason(artifact: dict[str, Any], current_cad: dict[str, Any]) -> str | None:
    """Return STALE_DECISION_REVISION when the artifact is bound to an old version."""
    artifact = artifact if isinstance(artifact, dict) else {}
    current_cad = current_cad if isinstance(current_cad, dict) else {}
    artifact_version = _string(artifact.get("decision_version_id"))
    if not artifact_version:
        return None
    current_version = _string(current_cad.get("decision_version_id"))
    if artifact_version != current_version:
        return "STALE_DECISION_REVISION"
    return None


def approval_binds_revision(approval_artifact: dict[str, Any], current_cad: dict[str, Any]) -> bool:
    """Approval may only authorize the exact decision version it was granted for."""
    return artifact_version_matches(approval_artifact, current_cad)


# Module-level default ledger for bounded/local flows; production wiring passes
# an explicit ledger instance (or a store-backed one) per decision lineage.
_DEFAULT_LEDGER = DecisionRevisionLedger()


def build_canonical_decision_for_stage(
    *,
    business_reasoning_result: dict[str, Any] | None,
    situation_understanding: dict[str, Any] | None = None,
    case_context_pack: dict[str, Any] | None = None,
    intake_result: dict[str, Any] | None = None,
    case_id: str = "",
    situation_version: str = "",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """One-shot stage helper: returns ``(canonical_decision, canonicalization_failure)``.

    Exactly one of the two values is non-None when a proposal exists; both are
    None for BR actions outside the first enforced slice (legacy path).
    """
    proposal = build_business_decision_proposal(business_reasoning_result)
    if proposal is None:
        return None, None
    outcome = canonicalize(
        proposal=proposal,
        situation_understanding=situation_understanding,
        case_context_pack=case_context_pack,
        intake_result=intake_result,
        case_id=case_id,
        situation_version=situation_version,
    )
    if outcome.get("semantic_status") == "FROZEN":
        return outcome, None
    return None, outcome


__all__ = [
    "ACTION_TYPE_CHANNELS",
    "ACTION_TYPE_DEFAULT_TARGET",
    "BUSINESS_DECISION_PROPOSAL_SCHEMA_VERSION",
    "CANONICAL_ACTION_DECISION_SCHEMA_VERSION",
    "CANONICAL_ACTION_TYPES",
    "CANONICAL_CHANNELS",
    "CANONICAL_GOALS",
    "CANONICAL_TARGETS",
    "CANONICALIZATION_FAILURE_SCHEMA_VERSION",
    "DECISION_REVISION_EVENT_SCHEMA_VERSION",
    "DECISION_REVISION_REQUEST_SCHEMA_VERSION",
    "DecisionRevisionError",
    "DecisionRevisionLedger",
    "FAILURE_REASON_CODES",
    "REVISION_OBSERVABILITY_CODES",
    "REVISION_REASON_CODES",
    "REVISION_REQUEST_STATUSES",
    "REVISION_STATUSES",
    "approval_binds_revision",
    "artifact_version_matches",
    "build_business_decision_proposal",
    "build_canonical_decision_for_stage",
    "build_decision_revision_request",
    "canonical_decision_code",
    "canonicalization_failure_review_state",
    "canonicalize",
    "decision_version_id_of",
    "evaluate_decision_revision",
    "request_decision_revision",
    "semantic_hash_of",
    "semantic_signature_matches",
    "stale_artifact_reason",
]
