"""Minimal deterministic segmentation of inbound email body content.

P0.5 provenance residual: the inbound body is safe as a whole
(``instruction_authority = NONE``), but granular provenance of which part is
the current customer message, quoted history or forwarded message was lost.

This module adds a small deterministic (no ML, no full MIME parser)
segmentation sufficient to build ``MessageSegment`` records with the shared
``evidence_authority`` provenance dimensions.

Rule that must never be violated: quoted and forwarded content keep
``instruction_authority = NONE`` regardless of the text inside them, even if
that text impersonates an operator ("administrator approved", "run tool X").
Forwarded/quoted content is evidence, never a runtime instruction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SEGMENT_TYPES = ("current", "quoted", "forwarded")

_FORWARDED_MARKERS = (
    "---------- forwarded message ----------",
    "---------- przekazana wiadomosc ----------",
    "---------- przekazywana wiadomosc ----------",
)

_QUOTED_STRONG_MARKERS = (
    "wrote:",
    "napisal:",
    "napisał:",
    "napisała:",
)

_QUOTED_HEADER_PREFIXES = (
    "-----original message-----",
    "od:",
    "from:",
    "sent:",
    "wysłano:",
    "wyslano:",
    "date:",
    "do:",
    "to:",
    "subject:",
    "tematu:",
)

_ORIGIN_BY_SEGMENT = {
    "current": "CUSTOMER_EMAIL",
    "quoted": "QUOTED_CONTENT",
    "forwarded": "FORWARDED_CONTENT",
}


@dataclass(frozen=True)
class MessageSegment:
    """One segment of an inbound email body with provenance dimensions."""

    segment_type: str
    text: str
    source_origin: str
    evidence_authority: str
    instruction_authority: str = "NONE"
    produced_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_type": self.segment_type,
            "text": self.text,
            "source_origin": self.source_origin,
            "evidence_authority": self.evidence_authority,
            "instruction_authority": self.instruction_authority,
            "produced_by": self.produced_by,
        }


def _evidence_for(origin: str) -> str:
    from evidence_authority import evidence_authority_for_origin

    return evidence_authority_for_origin(origin)


def _normalize_blocks(body_text: str) -> list[str]:
    text = str(body_text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = [block.strip() for block in text.split("\n\n") if block.strip()]
    return blocks or [text.strip()]


def _is_forwarded_start(block: str) -> bool:
    first = block.splitlines()[0].strip().lower() if block.splitlines() else ""
    return any(marker in first for marker in _FORWARDED_MARKERS)


def _is_quoted_start(block: str) -> bool:
    first = block.splitlines()[0].strip().lower() if block.splitlines() else ""
    if any(marker in first for marker in _QUOTED_STRONG_MARKERS):
        return True
    return any(first.startswith(prefix) for prefix in _QUOTED_HEADER_PREFIXES)


def segment_message(body_text: str | None) -> list[MessageSegment]:
    """Deterministic segmentation: current / quoted / forwarded.

    State machine: a forwarded marker starts a forwarded region that runs to
    the end; a quoted reply header starts a quoted region until a forwarded
    marker appears. All other content is the current customer message.
    """
    blocks = _normalize_blocks(body_text)
    segments: list[MessageSegment] = []
    state = "current"
    for block in blocks:
        if _is_forwarded_start(block):
            state = "forwarded"
        elif state != "forwarded" and _is_quoted_start(block):
            state = "quoted"
        origin = _ORIGIN_BY_SEGMENT[state]
        segments.append(
            MessageSegment(
                segment_type=state,
                text=block,
                source_origin=origin,
                evidence_authority=_evidence_for(origin),
                instruction_authority="NONE",
            )
        )
    if not segments:
        segments.append(
            MessageSegment(
                segment_type="current",
                text=str(body_text or ""),
                source_origin="CUSTOMER_EMAIL",
                evidence_authority=_evidence_for("CUSTOMER_EMAIL"),
                instruction_authority="NONE",
            )
        )
    return segments


def segment_message_dicts(body_text: str | None) -> list[dict[str, Any]]:
    return [segment.to_dict() for segment in segment_message(body_text)]


def attach_message_segments(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Production seam: segment the inbound body exactly once and attach the
    structured segments additively to ``source_message``.

    ``body_text``/``snippet`` are left untouched (legacy consumers keep working
    on the raw blob). If segments are already present, nothing is recomputed.
    Returns a new snapshot dict when changed; never mutates the input.
    """
    snap = dict(snapshot) if isinstance(snapshot, dict) else {}
    source_message = snap.get("source_message")
    if not isinstance(source_message, dict) or "message_segments" in source_message:
        return snap
    body = str(
        source_message.get("body_text") or source_message.get("body") or ""
    ).strip()
    if not body:
        return snap
    snap["source_message"] = {
        **source_message,
        "message_segments": segment_message_dicts(body),
    }
    return snap


__all__ = [
    "SEGMENT_TYPES",
    "MessageSegment",
    "attach_message_segments",
    "segment_message",
    "segment_message_dicts",
]
