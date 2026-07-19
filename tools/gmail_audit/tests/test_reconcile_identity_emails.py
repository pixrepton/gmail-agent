"""Tests for P1 email-identical identity dedup."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from correlation_registry.identity_email_dedup import run_email_identity_dedup
from correlation_registry.store import InMemoryCorrelationRegistryStore


def _seed_duplicate_email_store() -> InMemoryCorrelationRegistryStore:
    store = InMemoryCorrelationRegistryStore()
    older = store.create_identity(email="dup@example.com", display_name="Older")
    newer = store.create_identity(email="dup@example.com", display_name="Newer")
    eng_old = store.resolve_or_create_engagement(identity_id=older)
    eng_new = store.resolve_or_create_engagement(identity_id=newer)
    store.upsert_link(
        engagement_id=eng_old,
        link_type="mailbox_case",
        target_id="case_old",
        source_repo="gmail-agent",
    )
    store.upsert_link(
        engagement_id=eng_new,
        link_type="mailbox_case",
        target_id="case_new",
        source_repo="gmail-agent",
    )
    assert older != newer
    return store


def test_find_duplicate_email_groups_in_memory() -> None:
    store = _seed_duplicate_email_store()
    groups = store.find_duplicate_email_groups()
    assert len(groups) == 1
    assert groups[0]["email_norm"] == "dup@example.com"
    assert groups[0]["identity_count"] == 2


def test_email_dedup_dry_run_does_not_merge() -> None:
    store = _seed_duplicate_email_store()
    result = run_email_identity_dedup(store, dry_run=True)
    assert result["duplicate_groups_before"] == 1
    assert result["planned_groups"] == 1
    assert result["merged_groups"] == 0
    assert store.count_duplicate_email_groups() == 1


def test_email_dedup_apply_merges_and_repoints_engagements() -> None:
    store = _seed_duplicate_email_store()
    before_groups = store.find_duplicate_email_groups()
    canonical = before_groups[0]["identity_ids"][0]
    result = run_email_identity_dedup(store, dry_run=False, limit=1)
    assert result["merged_groups"] == 1
    assert result["duplicate_groups_after"] == 0
    assert result["engagements_repointed"] >= 1
    assert store.count_duplicate_email_groups() == 0
    for engagement in store.engagements.values():
        assert engagement["identity_id"] == canonical


# Phase P1 proof token (gate): IDENTITY_EMAIL_DEDUP_PROOF_OK
