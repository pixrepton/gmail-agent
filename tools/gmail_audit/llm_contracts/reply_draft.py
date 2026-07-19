"""Pydantic contract for the reply_drafter stage output."""

from __future__ import annotations

from pydantic import BaseModel


class ReplyDraftItem(BaseModel):
    variant: str
    subject_suggestion: str
    body: str
    goal: str
    tone: str | None = None

    model_config = {"extra": "ignore"}


class ReplyDraftResult(BaseModel):
    draft_enabled: bool
    drafts: list[ReplyDraftItem]
    do_not_send_reasons: list[str]
    recommended_variant: str | None = None
    requires_manual_edit: bool | None = None
    unsafe_claims_detected: bool | None = None
    confidence: float | None = None

    model_config = {"extra": "ignore"}
