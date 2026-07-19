"""Pydantic contract for the intake_second_pass supplement output."""

from __future__ import annotations

from pydantic import BaseModel


class IntakeSecondPassResult(BaseModel):
    schema_version: str = "1.0"
    supplement_notes_pl: str
    suggested_review_escalation: bool
    additional_review_flags: list[str]
    evidence_assessment_pl: str

    model_config = {"extra": "ignore"}
