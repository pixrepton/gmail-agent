"""Structured logging with correlation context for TOP-INSTAL gmail-agent.

Uses ContextVar for thread-safe correlation tracking (case_id, engagement_id,
signal_id, source_kind, turn). All loggers emit JSON for Loki/ELK querying.

Usage:
    from log_config import get_logger, set_correlation, reset_correlation

    logger = get_logger(__name__)
    tokens = set_correlation(case_id=case.id, signal_id=signal.id)
    try:
        logger.info("EVENT_NAME", extra={"x": {"key": "val"}})
    finally:
        reset_correlation(tokens)
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any

# ── Correlation context ─────────────────────────────────────────────

_case_id: ContextVar[str] = ContextVar("case_id", default="")
_engagement_id: ContextVar[str] = ContextVar("engagement_id", default="")
_signal_id: ContextVar[str] = ContextVar("signal_id", default="")
_source_kind: ContextVar[str] = ContextVar("source_kind", default="")
_turn: ContextVar[int] = ContextVar("turn", default=0)
_trace_id: ContextVar[str] = ContextVar("trace_id", default="")  # Faza 0c: distributed tracing

_CORRELATION_VARS = (
    _case_id,
    _engagement_id,
    _signal_id,
    _source_kind,
    _turn,
    _trace_id,
)


def set_correlation(
    case_id: str = "",
    engagement_id: str = "",
    signal_id: str = "",
    source_kind: str = "",
    turn: int = 0,
) -> list[Any]:
    """Ustaw correlation context. Zwraca tokeny do reset_correlation()."""
    return [
        _case_id.set(case_id),
        _engagement_id.set(engagement_id),
        _signal_id.set(signal_id),
        _source_kind.set(source_kind),
        _turn.set(turn),
    ]


def set_trace_id(tid: str) -> None:
    """Ustaw trace_id dla distributed tracing."""
    _trace_id.set(tid)


def get_trace_id() -> str:
    """Pobierz aktualny trace_id."""
    return _trace_id.get()


def reset_correlation(tokens: list[Any]) -> None:
    for var, token in zip(_CORRELATION_VARS, tokens):
        var.reset(token)


# ── Logging filter & formatter ──────────────────────────────────────


class CorrelationFilter(logging.Filter):
    """Dodaje correlation fields (case_id, signal_id, trace_id, itd.) do każdego LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.case_id = _case_id.get()
        record.engagement_id = _engagement_id.get()
        record.signal_id = _signal_id.get()
        record.source_kind = _source_kind.get()
        record.turn = _turn.get()
        record.trace_id = _trace_id.get()
        return True


class JSONFormatter(logging.Formatter):
    """Formatuje log jako JSON z correlation fields i extras."""

    def format(self, record: logging.LogRecord) -> str:
        base: dict[str, Any] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "case_id": getattr(record, "case_id", ""),
            "engagement_id": getattr(record, "engagement_id", ""),
            "signal_id": getattr(record, "signal_id", ""),
            "source_kind": getattr(record, "source_kind", ""),
            "turn": getattr(record, "turn", 0),
            "trace_id": getattr(record, "trace_id", ""),
        }
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "x", {})
        if extra:
            base.update(extra)
        return json.dumps(base, default=str)


def get_logger(name: str) -> logging.Logger:
    """Zwraca logger z JSONFormatter i CorrelationFilter.

    Dodaje handler tylko jeśli logger jeszcze go nie ma (unikamy duplikacji).
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        handler.addFilter(CorrelationFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
