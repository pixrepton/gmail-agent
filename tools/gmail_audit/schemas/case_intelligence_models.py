from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


class RiskType(str, Enum):
    LEAD_LOSS = "lead_loss"
    OPERATIONAL_DELAY = "operational_delay"
    LOGISTICS = "logistics"
    FINANCE = "finance"
    AGING = "aging"
    CUSTOMER_SILENCE = "customer_silence"
    SUPPLIER_DEPENDENCY = "supplier_dependency"


class RiskAssessment(BaseModel):
    risk_type: RiskType
    severity: float = Field(ge=0, le=1)
    description_pl: str
    mitigation_pl: Optional[str] = None


class MissingInfo(BaseModel):
    field_name: str
    required_for: str
    suggested_source: Optional[str] = None


class NextBestAction(BaseModel):
    action_type: str
    description_pl: str
    urgency: str = "normal"
    prerequisites: list[str] = Field(default_factory=list)


class CaseUnderstanding(BaseModel):
    summary_pl: str
    customer_intent: Optional[str] = None
    key_entities: list[str] = Field(default_factory=list)
    last_significant_event: Optional[str] = None


class DeskComposition(BaseModel):
    primary_desk: str = "inbox"
    suggested_tags: list[str] = Field(default_factory=list)


class LifecycleRevision(BaseModel):
    stage: str
    confidence: float = Field(ge=0, le=1)
    reason_pl: str


class CaseIntelligenceResult(BaseModel):
    case_id: str
    understanding: CaseUnderstanding
    missing_info: list[MissingInfo] = Field(default_factory=list)
    risks: list[RiskAssessment] = Field(default_factory=list)
    next_best_action: Optional[NextBestAction] = None
    desk_composition: Optional[DeskComposition] = None
    lifecycle_revision: Optional[LifecycleRevision] = None
    merge_split_suggestions: list[dict] = Field(default_factory=list)

    model_config = {"use_enum_values": True}
