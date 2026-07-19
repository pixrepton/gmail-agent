"""Structured output for adaptive LLM projection composer (read-only)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProjectionComposeResult(BaseModel):
    """Bounded enrichment for ProjectionEnvelope — no writes, no execute."""

    essence_summary_pl: str = ""
    desk_card_title_pl: str = ""
    operator_visibility_note_pl: str = ""
    warnings: list[str] = Field(default_factory=list)

    model_config = {"extra": "ignore"}


__all__ = ["ProjectionComposeResult"]
