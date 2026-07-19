"""Structured output for personalized outbound offer email."""

from __future__ import annotations

from pydantic import BaseModel, Field


class EmailPersonalizationResult(BaseModel):
    subject: str = ""
    body: str = ""
    tone_used: str = Field(default="", description="Audit field describing communication tone applied.")

    model_config = {"extra": "ignore"}
