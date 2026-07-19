"""Structured output for HVAC signal extraction from inbound mail."""

from __future__ import annotations

from pydantic import BaseModel


class SignalExtractionResult(BaseModel):
    hvac_intent: str = ""
    building_type: str = ""
    heated_area_m2: float | None = None
    construction_year: int | None = None
    current_heating_source: str | None = None
    budget_pln_estimated: float | None = None
    price_sensitivity: str | None = None
    raw_geographic_signal: str | None = None
    model_config = {"extra": "ignore"}
