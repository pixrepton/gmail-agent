"""Structured output for Skrzat operator copilot (read-only case Q&A)."""



from __future__ import annotations



from typing import Any



from pydantic import BaseModel, Field





class SkrzatAnswerResult(BaseModel):

    answer_text: str = ""

    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)

    gap_refs: list[dict[str, Any]] = Field(default_factory=list)

    conflict_refs: list[dict[str, Any]] = Field(default_factory=list)

    warnings: list[str] = Field(default_factory=list)

    operator_caution_pl: str = ""



    model_config = {"extra": "ignore"}
