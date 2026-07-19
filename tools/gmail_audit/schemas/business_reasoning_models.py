from pydantic import BaseModel, Field
from typing import Optional


class BusinessReasoningResult(BaseModel):
    business_area: str
    customer_state: Optional[str] = None
    recommended_next_action: str
    priority: str = "normal"
    risks_summary_pl: Optional[str] = None
    overall_confidence: float = Field(ge=0, le=1)
    operator_note_pl: Optional[str] = None
    requires_immediate_attention: bool = False
