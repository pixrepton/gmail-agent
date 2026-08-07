"""OperatorCommand DTO for agent-chat command spine (AI-OS 6.3)."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class OperatorCommand:
    user_input: str
    session_id: str
    case_id: str = ""
    operator_id: str = "default"
    command_id: str = field(default_factory=lambda: f"cmd_{uuid.uuid4().hex[:16]}")
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=_utc_now_iso)
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        if not str(self.command_id or "").strip():
            self.command_id = f"cmd_{uuid.uuid4().hex[:16]}"
        if not self.idempotency_key:
            basis = "|".join(
                [
                    self.session_id,
                    self.case_id,
                    self.operator_id,
                    hashlib.sha256(self.user_input.encode("utf-8")).hexdigest()[:32],
                ]
            )
            self.idempotency_key = f"operator_command:{hashlib.sha256(basis.encode('utf-8')).hexdigest()}"

    def to_signal_payload(self, *, operator_memory_context: str = "") -> dict[str, Any]:
        return {
            "user_input": self.user_input,
            "session_id": self.session_id,
            "case_id": self.case_id,
            "command_id": self.command_id,
            "correlation_id": self.correlation_id,
            "operator_id": self.operator_id,
            "operator_memory_context": operator_memory_context,
        }


__all__ = ["OperatorCommand"]
