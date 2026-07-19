"""Unified OS event spine — domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

ProcessingStatus = Literal["pending", "processing", "processed", "failed", "skipped"]
TerminalStatus = Literal["processed", "failed", "skipped"]
ProcessorMode = Literal["off", "shadow", "active"]
Severity = Literal["debug", "info", "warn", "error", "critical"]


@dataclass(frozen=True)
class OsEvent:
    event_id: str
    event_type: str
    engagement_id: str | None
    source_repo: str
    occurred_at: datetime
    payload: dict[str, Any]
    correlation: dict[str, Any]
    processing_status: ProcessingStatus
    attempt_count: int
    processor_id: str | None = None
    last_error: str | None = None
    failure_detail: dict[str, Any] = field(default_factory=dict)
    # P7 Observability fields (OpenTelemetry-compatible)
    trace_id: str | None = None
    span_id: str | None = None
    parent_event_id: str | None = None
    case_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    severity: str = "info"
    duration_ms: int | None = None
    token_usage: dict[str, Any] | None = None
    cost: float | None = None
    success: bool | None = None
    error_message: str | None = None


@dataclass
class HandlerResult:
    outcome: TerminalStatus
    message: str = ""
    detail: dict[str, Any] | None = None


@dataclass
class ProcessBatchResult:
    claimed: int = 0
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
