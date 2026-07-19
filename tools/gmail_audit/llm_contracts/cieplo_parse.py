"""Structured output for cieplo.app HTML fact extraction."""

from __future__ import annotations

from pydantic import BaseModel


class CieploParseResult(BaseModel):
    calculated_power_kw: float | None = None
    design_temperature_zone: str = ""
    heated_floor_area_m2: float | None = None
    building_ventilation_type: str = ""
    thickness_of_insulation_cm: float | None = None
    total_energy_demand_kwh_year: float | None = None

    model_config = {"extra": "ignore"}
