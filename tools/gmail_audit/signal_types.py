"""Canonical signal-runtime type aliases and constants."""

from __future__ import annotations

from typing import Literal


SIGNAL_SCHEMA_VERSION = "1.0"

SignalRuntimeMode = Literal["legacy", "shadow", "active"]
SignalSourceKind = Literal["gmail", "drive"]
SignalProcessingState = Literal["pending", "reconciled", "skipped_duplicate", "failed", "shadowed"]

GMAIL_SIGNAL_KINDS = (
    "gmail_message_observed",
    "gmail_thread_update_observed",
    "gmail_attachment_observed",
)

DRIVE_SIGNAL_KINDS = (
    "drive_document_added",
    "drive_document_updated",
    "drive_media_batch_observed",
    "drive_document_removed",
    "drive_document_link_candidate",
    "drive_extraction_completed",
    "drive_conflict_detected",
)

ALL_SIGNAL_KINDS = GMAIL_SIGNAL_KINDS + DRIVE_SIGNAL_KINDS

SOURCE_CURSOR_STATUS = ("idle", "running", "ok", "error")

__all__ = [
    "ALL_SIGNAL_KINDS",
    "DRIVE_SIGNAL_KINDS",
    "GMAIL_SIGNAL_KINDS",
    "SIGNAL_SCHEMA_VERSION",
    "SOURCE_CURSOR_STATUS",
    "SignalProcessingState",
    "SignalRuntimeMode",
    "SignalSourceKind",
]
