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


class ToolCallItem(StrictModel):
    tool: str
    status: str = "idle"


class MaterializeProposalItem(StrictModel):
    proposal_id: str
    proposal_type: Literal[
        "link_existing",
        "create_case",
        "create_artifact",
        "defer_operator",
        "composite_plan",
    ]
    payload_json: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "approved", "rejected"] = "pending"


class AgentMemory(StrictModel):
    reasoning_trace: list[ReasoningTraceItem] = Field(default_factory=list)
    tool_calls: list[ToolCallItem] = Field(default_factory=list)
    constitution_sections_used: list[str] = Field(default_factory=list)
    materialize_proposals: list[MaterializeProposalItem] = Field(default_factory=list)


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


class EngagementSnapshotV2(StrictModel):
    engagement_id: str
    case_id: str
    version: int = Field(ge=1)
    signal_id: str = ""
    trace_id: str = ""
    operational_status: OperationalStatus
    hvac_profile: HvacProfile = Field(default_factory=HvacProfile)
    gaps: list[GapItem] = Field(default_factory=list)
    agent_memory: AgentMemory = Field(default_factory=AgentMemory)
    actions: list[ActionItem] = Field(default_factory=list)
    user_instruction: str | None = None
    hitl_gate: HitlGate = Field(default_factory=lambda: HitlGate(required=False, reason=""))
    case_kind: CaseKindLiteral = "niezaklasyfikowane"
    case_understanding: CaseUnderstandingProjection | None = None


def engagement_snapshot_v2_json_schema() -> dict[str, Any]:
    return EngagementSnapshotV2.model_json_schema()
