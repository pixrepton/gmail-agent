"""
Backend Policy Engine (V2.1 Layer 6 slice) — rule-based guards for intake actions.

BOUNDARY (non-negotiable):
- This module evaluates policy and consumes *authoritative business verdicts* when present.
- It does NOT compute HVAC sizing, pricing, margin, or OfferDTO semantics — those belong in kalk-top
  and other authoritative systems. Missing authority => downgrade/block, never invented truth.

Daszek remains projection-only; policy runs in Python backend, not in WordPress UI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from log_config import get_logger

logger = get_logger(__name__)

from case_snapshot_hot_state_contract import is_evidence_backed_fact

POLICY_REPORT_SCHEMA_VERSION = "policy_report.v1"

PolicyStatus = Literal["APPROVED", "REJECTED", "NEEDS_HUMAN"]
PolicyRiskClass = Literal["low", "medium", "high", "critical"]
RuleFamily = Literal["operational", "communication", "trust", "business_invariant"]


@dataclass(slots=True)
class BusinessInvariantVerdictRef:
    """Reference to an authoritative verdict produced outside gmail-agent (e.g. kalk-top, review workflow)."""

    verdict_kind: str
    """e.g. commercial_margin, technical_sizing, compliance_checklist"""

    source_system: str
    """Logical owner id — must not be 'gmail_agent_inferred'."""

    status: str
    """e.g. pass, fail, stale, pending"""

    record_id: str = ""
    observed_at: str = ""


@dataclass(slots=True)
class PolicyContext:
    """Inputs beside the hot snapshot — thresholds, timeline, trust, external verdict handles."""

    trace_id: str = ""
    proposal_id: str = ""
    # Relationship / outreach
    is_first_contact: bool = False
    has_approved_communication_state: bool = False
    relationship_debtor: bool = False
    relationship_complaint_active: bool = False
    relationship_escalation_open: bool = False
    # Identity / link quality (from entity linker or snapshot meta)
    entity_link_status: str = ""
    # Trust
    source_trust_score: float = 1.0
    source_trust_threshold: float = 0.35
    min_confidence_for_autonomous_fact_action: float = 0.55
    snapshot_confidence: float = 0.0
    # Timeline: list of {"event_type": str, "occurred_at": iso str, "channel": str?}
    event_timeline: list[dict[str, Any]] = field(default_factory=list)
    follow_up_cooldown_hours: float = 72.0
    follow_up_event_types: tuple[str, ...] = (
        "follow_up_sent",
        "operator_follow_up",
        "outbound_follow_up",
        "communication_sent",
        "gmail.communication_sent",
    )
    # Conflicts
    conflict_severity_threshold: str = "high"
    # Authoritative verdicts (may also be extracted from snapshot_meta)
    authoritative_verdicts: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PolicyReport:
    schema_version: str = POLICY_REPORT_SCHEMA_VERSION
    trace_id: str = ""
    proposal_id: str = ""
    status: PolicyStatus = "APPROVED"
    effective_risk_class: PolicyRiskClass = "low"
    failed_rules: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    required_adjustments: list[str] = field(default_factory=list)
    policy_basis: list[str] = field(default_factory=list)
    requires_review: bool = False
    authoritative_inputs_used: list[str] = field(default_factory=list)
    authoritative_inputs_missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "proposal_id": self.proposal_id,
            "status": self.status,
            "effective_risk_class": self.effective_risk_class,
            "failed_rules": list(self.failed_rules),
            "warnings": list(self.warnings),
            "required_adjustments": list(self.required_adjustments),
            "policy_basis": list(self.policy_basis),
            "requires_review": self.requires_review,
            "authoritative_inputs_used": list(self.authoritative_inputs_used),
            "authoritative_inputs_missing": list(self.authoritative_inputs_missing),
        }


@dataclass(slots=True)
class PolicyRule:
    """Declarative rule metadata; evaluation is a pure function."""

    rule_id: str
    family: RuleFamily
    description: str
    evaluate: Callable[..., "_RuleEval | None"]


@dataclass(slots=True)
class _RuleEval:
    outcome: PolicyStatus
    risk: PolicyRiskClass
    message: str
    basis: str
    adjustments: list[str] = field(default_factory=list)


_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _max_risk(a: PolicyRiskClass, b: PolicyRiskClass) -> PolicyRiskClass:
    order = ("low", "medium", "high", "critical")
    return a if _SEVERITY_ORDER[a] >= _SEVERITY_ORDER[b] else b


def _parse_ts(s: str) -> datetime | None:
    raw = str(s or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def extract_authoritative_business_verdicts(
    case_snapshot_hot_state: dict[str, Any],
    policy_context: PolicyContext | None = None,
) -> dict[str, Any]:
    """
    Collect authoritative verdict blobs from hot state + context.

    Expected shapes (examples — owners are outside gmail-agent):
    - snapshot_meta.authoritative_verdicts: { commercial_margin: {...}, technical_sizing: {...}, ... }
    - case.metadata.authoritative_verdicts
    """
    out: dict[str, Any] = {}
    if policy_context is not None:
        ctx = policy_context.authoritative_verdicts
        if isinstance(ctx, dict):
            out.update({k: v for k, v in ctx.items() if v is not None})

    sm = case_snapshot_hot_state.get("snapshot_meta") if isinstance(case_snapshot_hot_state, dict) else None
    if isinstance(sm, dict):
        av = sm.get("authoritative_verdicts")
        if isinstance(av, dict):
            out.update({k: v for k, v in av.items() if v is not None})

    case = case_snapshot_hot_state.get("case") if isinstance(case_snapshot_hot_state, dict) else None
    if isinstance(case, dict):
        md = case.get("metadata")
        if isinstance(md, dict):
            av2 = md.get("authoritative_verdicts")
            if isinstance(av2, dict):
                out.update({k: v for k, v in av2.items() if v is not None})
    return out


def _verdict_ok(blob: Any) -> bool:
    if not isinstance(blob, dict):
        return False
    status = str(blob.get("status") or blob.get("verdict_status") or "").strip().lower()
    if status in {"pass", "approved", "ok", "authorized"}:
        return True
    if blob.get("authorized") is True:
        return True
    return False


def _has_verdict_kind(verdicts: dict[str, Any], kind: str) -> bool:
    key_aliases = {
        "commercial_margin": ("commercial_margin", "margin", "commercial", "authorized_margin"),
        "technical_sizing": ("technical_sizing", "sizing", "hvac_fit", "technical_fit"),
        "compliance_checklist": ("compliance_checklist", "subsidy_compliance", "compliance"),
    }
    keys = key_aliases.get(kind, (kind,))
    for k in keys:
        if k in verdicts and verdicts[k]:
            if _verdict_ok(verdicts[k]):
                return True
    return False


def _active_conflict_severities(case_snapshot_hot_state: dict[str, Any]) -> list[str]:
    ac = case_snapshot_hot_state.get("active_conflicts")
    if not isinstance(ac, list):
        return []
    out: list[str] = []
    for row in ac:
        if not isinstance(row, dict):
            continue
        sev = str(row.get("severity") or row.get("level") or "medium").strip().lower()
        out.append(sev)
    return out


def _max_conflict_severity(case_snapshot_hot_state: dict[str, Any]) -> str:
    sevs = _active_conflict_severities(case_snapshot_hot_state)
    if not sevs:
        return "low"
    best = "low"
    for s in sevs:
        if _SEVERITY_ORDER.get(s, 1) > _SEVERITY_ORDER.get(best, 0):
            best = s
    return best


# --- Rule implementations (pure; no kalk-top math) ---


def rule_no_live_send_to_new_clients(
    action_proposal: dict[str, Any],
    case_snapshot_hot_state: dict[str, Any],
    ctx: PolicyContext,
) -> _RuleEval | None:
    """NO_LIVE_SEND_TO_NEW_CLIENTS"""
    if str(action_proposal.get("action_class") or "").upper() != "LIVE_REPLY":
        return None
    if ctx.has_approved_communication_state:
        return None
    if ctx.is_first_contact or _snapshot_indicates_first_contact(case_snapshot_hot_state):
        return _RuleEval(
            outcome="NEEDS_HUMAN",
            risk="high",
            message="Live reply to new / first-contact thread requires human approval.",
            basis="NO_LIVE_SEND_TO_NEW_CLIENTS",
            adjustments=["Obtain approved communication state or operator send."],
        )
    return None


def _snapshot_indicates_first_contact(hot: dict[str, Any]) -> bool:
    la = hot.get("latest_activity")
    if isinstance(la, dict):
        if int(la.get("thread_message_count") or 0) <= 1:
            return True
        if la.get("is_first_contact") is True:
            return True
    sm = hot.get("snapshot_meta")
    if isinstance(sm, dict) and sm.get("first_contact") is True:
        return True
    return False


def rule_no_outbound_when_relationship_risk(
    action_proposal: dict[str, Any],
    case_snapshot_hot_state: dict[str, Any],
    ctx: PolicyContext,
) -> _RuleEval | None:
    """NO_OUTBOUND_WHEN_RELATIONSHIP_RISK_ACTIVE"""
    risk = ctx.relationship_debtor or ctx.relationship_complaint_active or ctx.relationship_escalation_open
    if not risk:
        case = case_snapshot_hot_state.get("case")
        if isinstance(case, dict):
            md = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
            risk = bool(
                md.get("debtor")
                or md.get("complaint_active")
                or md.get("service_escalation_open")
            )
    if not risk:
        return None
    comm = str(action_proposal.get("communication_intent") or "").lower()
    ac = str(action_proposal.get("action_class") or "").upper()
    if comm in {"marketing", "prospecting", "satisfaction_survey", "upsell"} or (
        ac == "OUTBOUND_MARKETING"
    ):
        return _RuleEval(
            outcome="REJECTED",
            risk="critical",
            message="Outbound marketing-style communication blocked while relationship risk is active.",
            basis="NO_OUTBOUND_WHEN_RELATIONSHIP_RISK_ACTIVE",
            adjustments=["Use human-handled recovery / complaint workflow."],
        )
    if ac in {"LIVE_REPLY", "OUTBOUND_STATUS"} and comm not in {"service_recovery", "complaint_ack"}:
        return _RuleEval(
            outcome="NEEDS_HUMAN",
            risk="high",
            message="Outbound action while debtor/complaint/escalation requires human routing.",
            basis="NO_OUTBOUND_WHEN_RELATIONSHIP_RISK_ACTIVE",
        )
    return None


def rule_no_repeat_follow_up_too_soon(
    action_proposal: dict[str, Any],
    _case_snapshot_hot_state: dict[str, Any],
    ctx: PolicyContext,
) -> _RuleEval | None:
    """NO_REPEAT_FOLLOWUP_TOO_SOON"""
    if str(action_proposal.get("action_class") or "").upper() not in {"FOLLOW_UP", "FOLLOW_UP_MESSAGE"}:
        return None
    if not ctx.event_timeline:
        return None
    now = datetime.now(timezone.utc)
    cooldown = float(ctx.follow_up_cooldown_hours) * 3600.0
    types = {t.lower() for t in ctx.follow_up_event_types}
    last_ts: datetime | None = None
    for ev in ctx.event_timeline:
        if not isinstance(ev, dict):
            continue
        et = str(ev.get("event_type") or "").lower()
        if et not in types:
            continue
        ts = _parse_ts(str(ev.get("occurred_at") or ""))
        if ts and (last_ts is None or ts > last_ts):
            last_ts = ts
    if last_ts is None:
        return None
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    delta = (now - last_ts).total_seconds()
    if delta < cooldown:
        return _RuleEval(
            outcome="REJECTED",
            risk="medium",
            message="Follow-up cooldown not elapsed for similar outbound.",
            basis="NO_REPEAT_FOLLOWUP_TOO_SOON",
            adjustments=[f"Wait until {ctx.follow_up_cooldown_hours}h after last follow-up."],
        )
    return None


def rule_no_fact_changing_without_authoritative_evidence(
    action_proposal: dict[str, Any],
    case_snapshot_hot_state: dict[str, Any],
    ctx: PolicyContext,
) -> _RuleEval | None:
    """NO_FACT_CHANGING_ACTION_WITHOUT_AUTHORITATIVE_EVIDENCE"""
    if not action_proposal.get("changes_external_state"):
        return None
    conf = float(ctx.snapshot_confidence or case_snapshot_hot_state.get("snapshot_meta", {}).get("confidence") or 0.0)
    if isinstance(case_snapshot_hot_state.get("snapshot_meta"), dict):
        conf = float(case_snapshot_hot_state["snapshot_meta"].get("confidence") or conf)
    kf = case_snapshot_hot_state.get("key_facts")
    evidence_ok = False
    if isinstance(kf, list) and kf:
        evidence_ok = any(is_evidence_backed_fact(row) for row in kf if isinstance(row, dict))
    if ctx.source_trust_score < ctx.source_trust_threshold:
        return _RuleEval(
            outcome="NEEDS_HUMAN",
            risk="high",
            message="Fact-changing action blocked: source trust below threshold.",
            basis="NO_FACT_CHANGING_ACTION_WITHOUT_AUTHORITATIVE_EVIDENCE",
        )
    if conf < ctx.min_confidence_for_autonomous_fact_action and not evidence_ok:
        return _RuleEval(
            outcome="NEEDS_HUMAN",
            risk="high",
            message="Fact-changing action requires authoritative evidence or higher confidence.",
            basis="NO_FACT_CHANGING_ACTION_WITHOUT_AUTHORITATIVE_EVIDENCE",
        )
    return None


def rule_no_offer_without_commercial_verdict(
    action_proposal: dict[str, Any],
    case_snapshot_hot_state: dict[str, Any],
    ctx: PolicyContext,
) -> _RuleEval | None:
    """NO_OFFER_RELATED_APPROVAL_WITHOUT_AUTHORIZED_BUSINESS_VERDICT / margin adapter."""
    ac = str(action_proposal.get("action_class") or "").upper()
    if ac not in {"OFFER_SEND", "OFFER_EXTERNAL", "COMMERCIAL_COMMIT"}:
        return None
    verdicts = extract_authoritative_business_verdicts(case_snapshot_hot_state, ctx)
    if _has_verdict_kind(verdicts, "commercial_margin"):
        return None
    live = bool(action_proposal.get("is_live") or action_proposal.get("external_send"))
    if live:
        return _RuleEval(
            outcome="REJECTED",
            risk="critical",
            message="Offer-related live action blocked without authorized commercial verdict.",
            basis="NO_OFFER_ACTION_WITHOUT_AUTHORIZED_MARGIN_VERDICT",
        )
    return _RuleEval(
        outcome="NEEDS_HUMAN",
        risk="high",
        message="Offer-related action needs human review: missing authoritative commercial verdict.",
        basis="NO_OFFER_RELATED_APPROVAL_WITHOUT_AUTHORIZED_BUSINESS_VERDICT",
    )


def rule_no_technical_without_sizing_verdict(
    action_proposal: dict[str, Any],
    case_snapshot_hot_state: dict[str, Any],
    ctx: PolicyContext,
) -> _RuleEval | None:
    """NO_TECHNICAL_APPROVAL_WITHOUT_AUTHORIZED_SIZING_VERDICT"""
    if not action_proposal.get("requires_technical_authority"):
        return None
    verdicts = extract_authoritative_business_verdicts(case_snapshot_hot_state, ctx)
    if _has_verdict_kind(verdicts, "technical_sizing"):
        return None
    return _RuleEval(
        outcome="NEEDS_HUMAN",
        risk="high",
        message="Technically sensitive action requires authoritative sizing/fit verdict from owner system.",
        basis="NO_TECHNICAL_APPROVAL_WITHOUT_AUTHORIZED_SIZING_VERDICT",
    )


def rule_compliance_for_subsidy_send(
    action_proposal: dict[str, Any],
    case_snapshot_hot_state: dict[str, Any],
    ctx: PolicyContext,
) -> _RuleEval | None:
    """NO_SUBSIDY_OR_COMPLIANCE_SEND_WITHOUT_REQUIRED_COMPLIANCE_CHECKLIST"""
    if not action_proposal.get("requires_compliance_content"):
        return None
    verdicts = extract_authoritative_business_verdicts(case_snapshot_hot_state, ctx)
    if _has_verdict_kind(verdicts, "compliance_checklist"):
        return None
    return _RuleEval(
        outcome="NEEDS_HUMAN",
        risk="high",
        message="Compliance/subsidy outbound requires authoritative checklist verdict.",
        basis="NO_SUBSIDY_OR_COMPLIANCE_SEND_WITHOUT_REQUIRED_COMPLIANCE_CHECKLIST",
    )


def rule_conflict_escalation(
    action_proposal: dict[str, Any],
    case_snapshot_hot_state: dict[str, Any],
    ctx: PolicyContext,
) -> _RuleEval | None:
    """CONFLICT_ESCALATION_RULE"""
    if str(action_proposal.get("action_class") or "").upper() in {"OBSERVE", "INTERNAL_NOTE"}:
        return None
    max_sev = _max_conflict_severity(case_snapshot_hot_state)
    thr = str(ctx.conflict_severity_threshold or "high").lower()
    if _SEVERITY_ORDER.get(max_sev, 0) < _SEVERITY_ORDER.get(thr, 2):
        return None
    return _RuleEval(
        outcome="NEEDS_HUMAN",
        risk="high",
        message="Active conflicts at or above threshold — no autonomous outbound/live action.",
        basis="CONFLICT_ESCALATION_RULE",
    )


def rule_document_calendar_review_required(
    action_proposal: dict[str, Any],
    case_snapshot_hot_state: dict[str, Any],
    ctx: PolicyContext,
) -> _RuleEval | None:
    """Require human review for document/calendar blockers before external writes."""
    _ = ctx
    action_class = str(action_proposal.get("action_class") or "").upper()
    action_type = str(action_proposal.get("action_type") or action_proposal.get("primary_action") or "")
    if action_class in {"OBSERVE", "INTERNAL_NOTE"}:
        return None
    changes_external = bool(action_proposal.get("changes_external_state")) or action_class in {
        "EXTERNAL_WRITE",
        "LIVE_REPLY",
        "OUTBOUND_STATUS",
        "FOLLOW_UP",
    } or action_type == "create_calendar_event"
    if not changes_external:
        return None

    active_conflicts = case_snapshot_hot_state.get("active_conflicts")
    if isinstance(active_conflicts, list):
        for row in active_conflicts:
            if not isinstance(row, dict):
                continue
            if str(row.get("source_kind") or "") == "document_intelligence":
                severity = str(row.get("severity") or "medium").lower()
                if _SEVERITY_ORDER.get(severity, 2) >= _SEVERITY_ORDER["medium"]:
                    return _RuleEval(
                        outcome="NEEDS_HUMAN",
                        risk="high",
                        message="Document intelligence conflict requires operator review before external action.",
                        basis="DOCUMENT_OR_CALENDAR_REVIEW_REQUIRED",
                    )

    blockers = case_snapshot_hot_state.get("blockers")
    if isinstance(blockers, list):
        for row in blockers:
            if not isinstance(row, dict):
                continue
            source = str(row.get("source_kind") or "")
            blocker_id = str(row.get("blocker_id") or "")
            if source == "calendar" or blocker_id.startswith("calendar_"):
                return _RuleEval(
                    outcome="NEEDS_HUMAN",
                    risk="high",
                    message="Calendar scheduling conflict/review blocker requires operator review before external action.",
                    basis="DOCUMENT_OR_CALENDAR_REVIEW_REQUIRED",
                )
            if blocker_id == "unsupported_claims":
                return _RuleEval(
                    outcome="NEEDS_HUMAN",
                    risk="medium",
                    message="Unsupported reasoning claims require review before external action.",
                    basis="DOCUMENT_OR_CALENDAR_REVIEW_REQUIRED",
                )
    return None


def rule_identity_unresolved(
    action_proposal: dict[str, Any],
    case_snapshot_hot_state: dict[str, Any],
    ctx: PolicyContext,
) -> _RuleEval | None:
    """Block autonomous action when case/signal identity link is unresolved."""
    st = str(ctx.entity_link_status or "").upper()
    if not st:
        sm = case_snapshot_hot_state.get("snapshot_meta")
        if isinstance(sm, dict):
            st = str(sm.get("entity_link_status") or "").upper()
    if st not in {"PENDING_ADJUDICATION", "LINK_CONFLICT", "UNRESOLVED"}:
        return None
    if str(action_proposal.get("action_class") or "").upper() in {"OBSERVE", "INTERNAL_NOTE"}:
        return None
    return _RuleEval(
        outcome="NEEDS_HUMAN",
        risk="medium",
        message="Identity/case link unresolved — operator adjudication required before external action.",
        basis="IDENTITY_LINK_UNRESOLVED",
    )


def rule_trust_and_review_defaults(
    action_proposal: dict[str, Any],
    case_snapshot_hot_state: dict[str, Any],
    ctx: PolicyContext,
) -> _RuleEval | None:
    """Require human when review flagged or non-authoritative inference-only proposals."""
    if action_proposal.get("inference_only") and action_proposal.get("changes_external_state"):
        return _RuleEval(
            outcome="NEEDS_HUMAN",
            risk="medium",
            message="External state change cannot rely on inference-only proposals without authority.",
            basis="NON_AUTHORITATIVE_INFERENCE",
        )
    sm = case_snapshot_hot_state.get("snapshot_meta")
    if isinstance(sm, dict) and sm.get("review_required"):
        return _RuleEval(
            outcome="NEEDS_HUMAN",
            risk="medium",
            message="Snapshot marked review_required.",
            basis="REVIEW_REQUIRED_META",
        )
    return None


def load_policy_rules() -> list[PolicyRule]:
    """Default ordered rule set — operational/communication/trust/invariant."""
    return [
        PolicyRule(
            "NO_LIVE_SEND_TO_NEW_CLIENTS",
            "communication",
            "Block autonomous live send to first-contact threads.",
            rule_no_live_send_to_new_clients,
        ),
        PolicyRule(
            "NO_OUTBOUND_WHEN_RELATIONSHIP_RISK_ACTIVE",
            "communication",
            "Block inappropriate outbound under debtor/complaint/escalation.",
            rule_no_outbound_when_relationship_risk,
        ),
        PolicyRule(
            "NO_REPEAT_FOLLOWUP_TOO_SOON",
            "operational",
            "Cooldown for repeated follow-ups.",
            rule_no_repeat_follow_up_too_soon,
        ),
        PolicyRule(
            "NO_FACT_CHANGING_ACTION_WITHOUT_AUTHORITATIVE_EVIDENCE",
            "trust",
            "Fact-changing actions need evidence/trust/confidence.",
            rule_no_fact_changing_without_authoritative_evidence,
        ),
        PolicyRule(
            "NO_OFFER_RELATED_APPROVAL_WITHOUT_AUTHORIZED_BUSINESS_VERDICT",
            "business_invariant",
            "Commercial verdict presence for offer-class actions.",
            rule_no_offer_without_commercial_verdict,
        ),
        PolicyRule(
            "NO_TECHNICAL_APPROVAL_WITHOUT_AUTHORIZED_SIZING_VERDICT",
            "business_invariant",
            "Sizing verdict consumed from owner system only.",
            rule_no_technical_without_sizing_verdict,
        ),
        PolicyRule(
            "NO_SUBSIDY_OR_COMPLIANCE_SEND_WITHOUT_REQUIRED_COMPLIANCE_CHECKLIST",
            "business_invariant",
            "Compliance checklist verdict consumed from owner system only.",
            rule_compliance_for_subsidy_send,
        ),
        PolicyRule(
            "CONFLICT_ESCALATION_RULE",
            "trust",
            "Escalate when hot conflicts exceed threshold.",
            rule_conflict_escalation,
        ),
        PolicyRule(
            "DOCUMENT_OR_CALENDAR_REVIEW_REQUIRED",
            "trust",
            "Document/calendar review blockers prevent external actions.",
            rule_document_calendar_review_required,
        ),
        PolicyRule(
            "IDENTITY_LINK_UNRESOLVED",
            "operational",
            "No autonomous external action with unresolved identity link.",
            rule_identity_unresolved,
        ),
        PolicyRule(
            "TRUST_AND_REVIEW_DEFAULTS",
            "trust",
            "Inference-only / review_required handling.",
            rule_trust_and_review_defaults,
        ),
    ]


def evaluate_rule(
    rule: PolicyRule,
    action_proposal: dict[str, Any],
    case_snapshot_hot_state: dict[str, Any],
    policy_context: PolicyContext,
) -> _RuleEval | None:
    return rule.evaluate(action_proposal, case_snapshot_hot_state, policy_context)


class PolicyEngine:
    """Rule-based policy evaluation — no LLM, no kalk-top formulas."""

    def __init__(self, rules: list[PolicyRule] | None = None) -> None:
        self._rules = rules or load_policy_rules()

    def evaluate(
        self,
        action_proposal: dict[str, Any],
        case_snapshot_hot_state: dict[str, Any],
        policy_context: PolicyContext,
    ) -> PolicyReport:
        trace_id = str(policy_context.trace_id or action_proposal.get("trace_id") or "")
        proposal_id = str(policy_context.proposal_id or action_proposal.get("proposal_id") or "proposal")

        verdicts = extract_authoritative_business_verdicts(case_snapshot_hot_state, policy_context)
        used: list[str] = []
        missing: list[str] = []
        for key, blob in verdicts.items():
            if blob and _verdict_ok(blob):
                used.append(f"{key}:authorized")
            elif blob:
                used.append(f"{key}:present_not_authorized")
            else:
                missing.append(key)

        report = PolicyReport(
            trace_id=trace_id,
            proposal_id=proposal_id,
            authoritative_inputs_used=sorted(set(used)),
            authoritative_inputs_missing=sorted(set(missing)),
        )

        status: PolicyStatus = "APPROVED"
        risk: PolicyRiskClass = "low"
        for rule in self._rules:
            ev = evaluate_rule(rule, action_proposal, case_snapshot_hot_state, policy_context)
            if ev is None:
                continue
            report.failed_rules.append(rule.rule_id)
            report.policy_basis.append(f"{rule.rule_id}: {ev.basis}")
            if ev.message:
                report.warnings.append(ev.message)
            report.required_adjustments.extend(ev.adjustments)
            risk = _max_risk(risk, ev.risk)
            if ev.outcome == "REJECTED":
                status = "REJECTED"
            elif ev.outcome == "NEEDS_HUMAN" and status == "APPROVED":
                status = "NEEDS_HUMAN"

        report.effective_risk_class = risk
        report.status = status
        report.requires_review = status != "APPROVED"

        # Recompute authoritative missing for report clarity: business keys expected for action
        ac = str(action_proposal.get("action_class") or "").upper()
        if ac in {"OFFER_SEND", "OFFER_EXTERNAL", "COMMERCIAL_COMMIT"} and not _has_verdict_kind(verdicts, "commercial_margin"):
            if "commercial_margin_verdict" not in report.authoritative_inputs_missing:
                report.authoritative_inputs_missing.append("commercial_margin_verdict")
        if action_proposal.get("requires_technical_authority") and not _has_verdict_kind(verdicts, "technical_sizing"):
            if "technical_sizing_verdict" not in report.authoritative_inputs_missing:
                report.authoritative_inputs_missing.append("technical_sizing_verdict")
        if action_proposal.get("requires_compliance_content") and not _has_verdict_kind(verdicts, "compliance_checklist"):
            if "compliance_checklist_verdict" not in report.authoritative_inputs_missing:
                report.authoritative_inputs_missing.append("compliance_checklist_verdict")
        report.authoritative_inputs_missing = sorted(set(report.authoritative_inputs_missing))

        return report


def policy_report_allows_autonomous_action(report: PolicyReport | dict[str, Any]) -> bool:
    """True only when status is APPROVED and risk is not critical."""
    if isinstance(report, PolicyReport):
        d = report.to_dict()
    else:
        d = report
    return str(d.get("status")) == "APPROVED" and str(d.get("effective_risk_class")) != "critical"


__all__ = [
    "POLICY_REPORT_SCHEMA_VERSION",
    "BusinessInvariantVerdictRef",
    "PolicyContext",
    "PolicyEngine",
    "PolicyReport",
    "PolicyRule",
    "PolicyRiskClass",
    "PolicyStatus",
    "evaluate_rule",
    "extract_authoritative_business_verdicts",
    "load_policy_rules",
    "policy_report_allows_autonomous_action",
]
