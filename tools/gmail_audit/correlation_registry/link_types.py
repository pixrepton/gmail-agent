"""Canonical link_type values for P0 correlation_links."""

from __future__ import annotations

LINK_TYPES_P0 = frozenset(
    {
        "mailbox_case",
        "cieplo_workflow",
        "gmail_message",
        "gmail_thread",
        "cieplo_external_key",
        "case_external_ref",
        "canonical_trace",
        "identity_email",
    }
)

LINK_TYPES_P1_RESERVED = frozenset(
    {
        "signal_journal_entry",
        "workflow_event",
        "action_proposal",
        "event_spine_seq",
        "offer_snapshot",
        "calc_request_snapshot",
        "case_context_pack_ref",
        "merged_into",
        "linked_case",
    }
)


def normalize_link_type(value: str) -> str:
    token = str(value or "").strip().lower()
    if token in LINK_TYPES_P0 or token in LINK_TYPES_P1_RESERVED:
        return token
    raise ValueError(f"unsupported link_type: {value!r}")
