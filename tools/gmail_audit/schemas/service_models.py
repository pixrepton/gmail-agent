from pydantic import BaseModel, Field
from typing import Optional, Any


class UnderstandingOutput(BaseModel):
    schema_version: str = "understanding_output.v1"
    understanding_output_id: str = ""
    case_id: str = ""
    source_signal_id: str = ""
    summary_pl: str = ""
    created_at: str = ""
    essence_pl: str = ""
    customer_intent_pl: str = ""
    what_arrived_pl: str = ""
    what_is_new_pl: str = ""
    what_is_missing_pl: str = ""
    what_is_risk_pl: str = ""
    next_action_suggestion_pl: str = ""
    missing_fields: list[str] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    risk_items: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    context_quality: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)
    requires_operator_review: bool = False


class IntakeSnapshot(BaseModel):
    message_id: str = ""
    thread_id: str = ""
    subject: str = ""
    sender: str = ""
    sender_email: str = ""
    body_preview: str = ""
    snippet: str = ""
    date: str = ""
    has_attachments: bool = False


class MemoryRecord(BaseModel):
    id: str = ""
    memory_type: str = ""
    session_id: str = ""
    key: str = ""
    value: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
