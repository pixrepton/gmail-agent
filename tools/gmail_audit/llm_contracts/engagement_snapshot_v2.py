"""Pydantic contract for operator EngagementSnapshot.v2 (agent working memory)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperationalStatus(StrictModel):
    code: Literal[
        "raw_inquiry",
        "enriching",
        "ready_for_quote",
        "pending_operator",
        "node_a_error",
    ]
    steps_remaining: int = Field(ge=0)
    blocking: bool = False


class HvacLocation(StrictModel):
    city: str | None = None
    postal_code: str | None = None


class HvacProfile(StrictModel):
    heated_area_m2: int | None = None
    location: HvacLocation = Field(default_factory=HvacLocation)
    building_type: str | None = None
    heat_pump_series_hint: str | None = None
    thermal_demand_kw: float | None = None
    cp2025_eligible: bool | None = None


class GapItem(StrictModel):
    field: str
    severity: Literal["blocking", "warning"]
    ask_pl: str


class ActionItem(StrictModel):
    id: str
    enabled: bool
    payload_pl: str | None = None
    disabled_reason_pl: str | None = None
    parent_policy_decision_id: str = ""
    parent_action_proposal_v2_id: str = ""
    parent_decision_candidate_id: str = ""
    source_signal_id: str = ""
    # AI-OS-CANONICAL-DRAFT-IDENTITY-01: the artifact identity of this action, distinct
    # from the four parent/lineage refs above. `draft_id` names the opportunity/slot
    # (stable across re-runs and operator edits of the same case+signal+action);
    # `revision`/`body_hash` version its content (bumped on every real content change,
    # never silently overwritten). `identity_state` is honest about whether lineage
    # correlation actually succeeded this turn -- never fabricated when it didn't.
    draft_id: str = ""
    revision: int = 1
    body_hash: str = ""
    case_id: str = ""
    identity_state: Literal["complete", "identity_incomplete"] = "identity_incomplete"


class HitlGate(StrictModel):
    required: bool
    reason: str = ""


class ReasoningTraceItem(StrictModel):
    turn: int = Field(ge=0)
    summary_pl: str = ""


class UnderstandingRiskItem(StrictModel):
    risk_type: str = ""
    severity: str = "medium"
    summary_pl: str = ""


class CaseUnderstandingProjection(StrictModel):
    """Compact, projection-safe read of case_intelligence_result/understanding_output
    for the CURRENT turn's signal only (A1). Written atomically with the rest of
    the snapshot in the same CAS save, so its presence is by construction fresh
    and correlated: graph._ground_current_signal sets it only when a correlated
    Understanding was computed for this exact turn's signal, and clears it
    (never leaves it) when that turn had none — see agent_runtime/graph.py.
    """

    source_signal_id: str = ""
    generated_at: str = ""
    essence_pl: str = ""
    what_changed_pl: str = ""
    why_pl: str = ""
    missing_critical_fields: list[str] = Field(default_factory=list)
    risks: list[UnderstandingRiskItem] = Field(default_factory=list)
    recommended_next_step_pl: str = ""
    # Roadmap 1.3: preferred tool class for planner (not a hard tool binding).
    planner_action_hint: str = ""


class CaseUnderstandingProvenance(StrictModel):
    """SLICE-3A: `CaseUnderstandingProvenanceV1` — how the sibling `case_understanding` was produced.

    Deliberately a SEPARATE, small envelope rather than fields inside
    `CaseUnderstandingProjection`: the projection is Brain 1's semantic content, this is metadata
    about the run that produced it. Keeping them apart means a reader can trust the content
    without having to strip bookkeeping out of it, and there is still exactly one semantic
    representation of the case — no third model.

    Every field is optional with an honest empty default. An empty string means "not known",
    never a fabricated status: a snapshot written before this slice simply has
    `case_understanding_provenance = None` and gets no invented provenance.

    `corrected` is NOT `degraded`. A normalisation may be a harmless synonym rewrite or a real
    dictionary collision, and nothing in the current code distinguishes them. Until a severity
    contract exists, this records WHAT happened and refuses to editorialise about how bad it was.
    """

    schema_version: str = "v1"
    #: `not_required` is a normal outcome (a lane that deliberately skips heavy reasoning) and must
    #: never surface to the operator as a failure or a warning.
    availability: Literal["available", "unavailable", "not_required", ""] = ""
    source_mode: Literal[
        "model_result", "normalized_model_result", "fallback", "skipped_for_lane", ""
    ] = ""
    validation_state: Literal["clean", "corrected", ""] = ""
    source_signal_id: str = ""
    observed_at: str = ""
    reason_codes: list[str] = Field(default_factory=list)
    normalization_count: int = Field(default=0, ge=0)
    validation_error_count: int = Field(default=0, ge=0)


class CaseUnderstandingStatusV1(StrictModel):
    """SLICE-2C: how good our reasoning about this case currently is — status ONLY.

    `case_understanding_provenance` records the mechanics of the run that produced the
    Understanding (source mode, validation state, counts). This is the one-word operator-facing
    read derived from it, so consumers stop re-deriving "is our understanding good enough?" from
    provenance internals, each with its own rules.

    Hard boundary, and the reason this slice exists: **this field must never influence feed
    membership**. `feed_visibility` does not import or read it, and a `degraded` or `unavailable`
    status neither hides an existing card nor creates a new one. Understanding quality and desk
    membership are different questions with different owners; conflating them would let a reasoning
    failure silently remove real operator work from the desk.

    `reasoning_not_required` is a normal outcome, not a defect: some lanes deliberately skip heavy
    reasoning.

    `corrected` provenance does NOT map to `degraded` — see `CaseUnderstandingProvenance`, which
    deliberately refuses to editorialise about how bad a normalisation was. Only a genuine
    substitute (`source_mode="fallback"`) is reported as `degraded`.
    """

    schema_version: Literal["case_understanding_status.v1"] = "case_understanding_status.v1"
    status: Literal["ok", "degraded", "unavailable", "reasoning_not_required"]
    #: mirrors `CaseUnderstandingProvenance.source_mode`; empty means "not known", never fabricated
    source: str = ""
    reason: str = ""
    reason_codes: list[str] = Field(default_factory=list)
    source_signal_id: str = ""
    observed_at: str = ""
    #: age of the underlying Understanding in seconds when it could be computed; `None` otherwise
    age_seconds: int | None = None


class PolicyActionEnvelopeV1(StrictModel):
    """Bounded read-only projection of canonical MailboxMemory policy/action records."""

    schema_version: Literal["policy_action_envelope.v1"] = "policy_action_envelope.v1"
    decision_candidate_id: str = ""
    policy_decision_id: str = ""
    action_proposal_id: str = ""
    source_signal_id: str = ""
    source_message_id: str = ""
    policy_status: str = ""
    action_intent: str = ""
    allowed_by_policy: bool | None = None
    requires_operator_approval: bool | None = None
    freshness: Literal["current", "stale", "unavailable"] = "unavailable"
    proposal_status: str = ""
    reason_codes: list[str] = Field(default_factory=list)
    generated_at: str = ""
    expires_at: str = ""


class SemanticPolicyPlanConsistencyV1(StrictModel):
    """Detection-only observation; it never authorizes, blocks, or rewrites a tool plan."""

    schema_version: Literal["semantic_policy_plan_consistency.v1"] = (
        "semantic_policy_plan_consistency.v1"
    )
    status: Literal[
        "consistent",
        "conflicting",
        "missing_policy_envelope",
        "stale_policy_envelope",
        "missing_plan_correlation",
        "not_evaluable",
    ]
    reason_codes: list[str] = Field(default_factory=list)
    policy_decision_id: str = ""
    action_proposal_id: str = ""
    tool_name: str = ""
    mapping_classification: Literal[
        "EXHAUSTIVE_MAPPING_EXISTS",
        "PARTIAL_MAPPING_EXISTS",
        "NO_SAFE_MAPPING_EXISTS",
    ] = "NO_SAFE_MAPPING_EXISTS"


class DecisionDivergenceObservationV1(StrictModel):
    """Detection-only comparison of existing decision views and the selected tool.

    Different literals are not automatically conflicts: the three action surfaces
    and the two case-type surfaces have different owners and no exhaustive mapping.
    """

    schema_version: Literal["decision_divergence_observation.v1"] = (
        "decision_divergence_observation.v1"
    )
    status: Literal["divergence_detected", "not_evaluable", "missing_inputs"]
    action_tree_status: Literal[
        "divergence_detected",
        "same_literal",
        "not_evaluable",
        "missing_inputs",
    ]
    case_typing_status: Literal[
        "same_literal",
        "different_unmapped_literals",
        "missing_inputs",
    ]
    tool_relation_status: Literal["not_evaluable", "missing_inputs"]
    reason_codes: list[str] = Field(default_factory=list)
    source_signal_id: str = ""
    business_recommended_action: str = ""
    action_planner_primary_action: str = ""
    next_best_action_type: str = ""
    reply_draft_enabled: bool | None = None
    case_family: str = ""
    case_kind: str = ""
    tool_name: str = ""


class ToolCallItem(StrictModel):
    tool: str
    status: str = "idle"


class MaterializeProposalItem(StrictModel):
    proposal_id: str
    # Historical literals retained for snapshot deserialization; RP-28 retains
    # only composite_plan for new append/execute (see materialize.RETAINED_*).
    proposal_type: Literal[
        "link_existing",
        "create_case",
        "create_artifact",
        "defer_operator",
        "composite_plan",
    ]
    payload_json: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "approved", "rejected"] = "pending"


class ClarificationAnswerItem(StrictModel):
    ask_pl: str = ""
    answer_pl: str = ""
    operator_id: str = ""
    answered_at: str = ""


class AgentMemory(StrictModel):
    reasoning_trace: list[ReasoningTraceItem] = Field(default_factory=list)
    tool_calls: list[ToolCallItem] = Field(default_factory=list)
    constitution_sections_used: list[str] = Field(default_factory=list)
    materialize_proposals: list[MaterializeProposalItem] = Field(default_factory=list)
    clarification_answers: list[ClarificationAnswerItem] = Field(default_factory=list)


CaseKindLiteral = Literal[
    # Sprawy klienckie (zewnętrzne)
    "wycena_oferta",          # prośba o wycenę/ofertę — lead
    "awaria_naprawa",         # usterka, awaria, zgłoszenie klienta
    "przeglad_konserwacja",   # planowy przegląd / konserwacja / serwis
    "faktura_sprzedaz",       # faktury wystawiane przez nas klientom
    "zapytanie_klienta",      # lead — zapytanie przedofertowe
    # Sprawy administracyjno-wewnętrzne
    "ksiegowosc",             # biuro rachunkowe, bank, księgowość
    "faktura_zakup",          # faktury zakupowe / kosztowe
    "zakupy_materialow",      # zamówienia części, narzędzi, Allegro
    "szkolenie",              # szkolenia, webinary, zaproszenia
    # Pozostałe
    "inne",
    "niezaklasyfikowane",
]

# Jedno źródło prawdy nazw — reużywane przez router klasyfikacji i prompt agenta.
CASE_KINDS: tuple[str, ...] = (
    "wycena_oferta", "awaria_naprawa", "przeglad_konserwacja", "faktura_sprzedaz", "zapytanie_klienta",
    "ksiegowosc", "faktura_zakup", "zakupy_materialow", "szkolenie", "inne", "niezaklasyfikowane",
)
SALES_CASE_KINDS = frozenset({"wycena_oferta", "zapytanie_klienta"})        # tylko te pytają o metraż
SERVICE_CASE_KINDS = frozenset({"awaria_naprawa", "przeglad_konserwacja"})
ADMIN_CASE_KINDS = frozenset({"ksiegowosc", "faktura_zakup", "zakupy_materialow", "szkolenie"})


class FeedVisibility(StrictModel):
    """SLICE-2B: routing/projection metadata deciding operator MAIN-feed membership.

    This is NOT a new semantic truth about the case — it records how the signal was routed and
    why, so `snapshot exists` stops implying `belongs in the operator feed`. Optional and
    additive: snapshots written before this slice validate unchanged and are treated as
    `main_feed` by an explicit legacy fallback.
    """

    mode: Literal["hidden", "case_timeline_only", "main_feed", "attention_required"] = "main_feed"
    reason_codes: list[str] = Field(default_factory=list)
    source_lane: str = ""
    source_triage_class: str = ""
    operator_override: bool = False
    #: SLICE-2B1: an unresolved EXECUTION state that the snapshot's own executive fields cannot
    #: express (today: a HITL send that resolved to `outcome_unknown`, which lives in
    #: MailboxMemory under `decision_key` and has no `OperationalStatus.code` literal). Purely a
    #: visibility projection -- it never affects execution, retry, or the decision key.
    execution_attention: bool = False
    execution_attention_reason: str = ""


class CommunicationReceipt(StrictModel):
    """Approve ≠ send. Tracks manual-delivery pending vs observed Sent/outbound.

    Additive optional field — legacy snapshots without it validate as None / absent.
    """

    state: Literal["none", "ready_for_manual_send", "communication_sent"] = "none"
    sent_at: str = ""
    gmail_message_id: str = ""
    thread_id: str = ""
    draft_id: str = ""
    body_hash: str = ""
    draft_origin: Literal["brain1", "brain2_fallback", "legacy_unknown"] = "legacy_unknown"


class DraftLineageProvenance(StrictModel):
    """AI-OS 3.2: durable draft origin after Brain1 transport or Brain2 fallback."""

    draft_origin: Literal["brain1", "brain2_fallback", "legacy_unknown"] = "legacy_unknown"
    origin_correlation_id: str = ""
    origin_producer: str = ""
    origin_created_at: str = ""


class EngagementSnapshotV2(StrictModel):
    engagement_id: str
    case_id: str
    version: int = Field(ge=1)
    signal_id: str = ""
    trace_id: str = ""
    operational_status: OperationalStatus
    #: FG-02: ISO timestamp of when `operational_status.code` last changed.
    #: Empty on legacy rows; Follow-up Guardian / SLA projection prefer this over row `updated_at`.
    #: Written only by the engagement store on status-code change (or first insert) — not by
    #: unrelated saves (actions, feed, HITL, guardian proposal).
    lifecycle_state_since: str = ""
    hvac_profile: HvacProfile = Field(default_factory=HvacProfile)
    gaps: list[GapItem] = Field(default_factory=list)
    agent_memory: AgentMemory = Field(default_factory=AgentMemory)
    actions: list[ActionItem] = Field(default_factory=list)
    user_instruction: str | None = None
    hitl_gate: HitlGate = Field(default_factory=lambda: HitlGate(required=False, reason=""))
    case_kind: CaseKindLiteral = "niezaklasyfikowane"
    case_understanding: CaseUnderstandingProjection | None = None
    #: SLICE-3A: set and cleared in lockstep with `case_understanding` by
    #: `graph._ground_current_signal`, so the two can never describe different signals.
    case_understanding_provenance: CaseUnderstandingProvenance | None = None
    #: SLICE-2C: derived one-word status for the same Understanding, written and cleared in the
    #: same lockstep. Display and triage only — `feed_visibility` must not read it.
    case_understanding_status: CaseUnderstandingStatusV1 | None = None
    policy_action_envelope: PolicyActionEnvelopeV1 | None = None
    semantic_policy_plan_consistency: SemanticPolicyPlanConsistencyV1 | None = None
    decision_divergence_observation: DecisionDivergenceObservationV1 | None = None
    feed_visibility: FeedVisibility | None = None
    communication_receipt: CommunicationReceipt | None = None
    draft_lineage_provenance: DraftLineageProvenance | None = None


def engagement_snapshot_v2_json_schema() -> dict[str, Any]:
    return EngagementSnapshotV2.model_json_schema()
