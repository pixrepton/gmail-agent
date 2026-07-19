"""Pydantic contract for intake_reasoning stage output (intake_output_v1.json)."""

from __future__ import annotations

from pydantic import BaseModel


class IntakeSource(BaseModel):
    channel: str
    mailbox: str
    observed_at: str

    model_config = {"extra": "ignore"}


class IntakeMessage(BaseModel):
    message_id: str
    date: str
    sender: str
    to: list[str]
    subject: str
    has_attachments: bool
    cc: list[str] = []
    snippet: str | None = None
    labels: list[str] = []

    model_config = {"extra": "ignore"}


class IntakeLinkedCaseCandidate(BaseModel):
    case_key: str
    case_type: str
    match_confidence: float

    model_config = {"extra": "ignore"}


class IntakeThread(BaseModel):
    thread_id: str
    thread_position: str
    is_reply_or_forward: bool
    thread_summary: str
    linked_case_candidates: list[IntakeLinkedCaseCandidate] = []

    model_config = {"extra": "ignore"}


class IntakePrimarySignal(BaseModel):
    code: str
    name: str
    description: str
    business_significance: str

    model_config = {"extra": "ignore"}


class IntakeSecondarySignal(BaseModel):
    code: str
    name: str

    model_config = {"extra": "ignore"}


class IntakeStateChange(BaseModel):
    detected: bool
    from_state: str | None = None
    to_state: str | None = None

    model_config = {"extra": "ignore"}


class IntakeCaseAssessment(BaseModel):
    case_family: str
    is_new_case: bool
    state_detected: str
    state_change: IntakeStateChange

    model_config = {"extra": "ignore"}


class IntakeDecision(BaseModel):
    action: str
    action_rationale: str
    suggested_owner: str | None = None
    sla_hint: str | None = None

    model_config = {"extra": "ignore"}


class IntakeConfidence(BaseModel):
    signal_confidence: float
    case_link_confidence: float
    decision_confidence: float
    extraction_confidence: float

    model_config = {"extra": "ignore"}


class IntakeReview(BaseModel):
    required: bool
    flags: list[str]

    model_config = {"extra": "ignore"}


class IntakeEntities(BaseModel):
    people: list[str] = []
    organizations: list[str] = []
    locations: list[str] = []
    products: list[str] = []

    model_config = {"extra": "ignore"}


class IntakeExtractedDate(BaseModel):
    value: str
    kind: str

    model_config = {"extra": "ignore"}


class IntakeExtractedAmount(BaseModel):
    value: float
    currency: str
    kind: str

    model_config = {"extra": "ignore"}


class IntakeReferences(BaseModel):
    invoice_numbers: list[str] = []
    shipment_numbers: list[str] = []
    order_numbers: list[str] = []
    transaction_numbers: list[str] = []
    case_ids: list[str] = []

    model_config = {"extra": "ignore"}


class IntakeDeadline(BaseModel):
    date: str
    reason: str

    model_config = {"extra": "ignore"}


class IntakeLeadDetails(BaseModel):
    property_type: str | None = None
    floor_area_m2: float | None = None
    city: str | None = None
    county: str | None = None
    inquiry_source: str | None = None

    model_config = {"extra": "ignore"}


class IntakeExtractedData(BaseModel):
    entities: IntakeEntities
    dates: list[IntakeExtractedDate]
    amounts: list[IntakeExtractedAmount]
    references: IntakeReferences
    deadlines: list[IntakeDeadline]
    lead_details: IntakeLeadDetails | None = None

    model_config = {"extra": "ignore"}


class IntakeReasoningResult(BaseModel):
    schema_version: str = "1.0"
    source: IntakeSource
    message: IntakeMessage
    thread: IntakeThread
    business_area: str
    primary_signal: IntakePrimarySignal
    secondary_signals: list[IntakeSecondarySignal]
    case_assessment: IntakeCaseAssessment
    decision: IntakeDecision
    priority: str
    confidence: IntakeConfidence
    review: IntakeReview
    reason: str | None = None
    extracted_data: IntakeExtractedData

    model_config = {"extra": "ignore"}
