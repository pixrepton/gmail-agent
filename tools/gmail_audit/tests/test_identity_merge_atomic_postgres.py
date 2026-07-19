"""Real-Postgres regression test for the identity-merge atomic transaction fix.

Reproduces the exact failure mode found during the Phase 0 D1 live-API proof: approving
an identity binding suggestion performed a real, durable mutation (engagement repoint +
source identity delete) but then raised `psycopg.errors.ForeignKeyViolation` while writing
`identity_merge_log`, because deleting the source identity had already cascade-deleted the
`identity_binding_suggestions` row the log insert referenced. The in-memory fake store used
by the rest of the D1 test suite does not model real FK/cascade behavior, so it cannot catch
this class of bug -- hence a real Postgres test.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from correlation_registry.identity_binding import SUGGESTION_APPROVED, execute_identity_merge
from correlation_registry.store import PostgresCorrelationRegistryStore

POSTGRES_TEST_DATABASE_URL = os.getenv("MAILBOX_MEMORY_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL, reason="MAILBOX_MEMORY_TEST_DATABASE_URL is not set"
)


def _seed_approved_suggestion(store: PostgresCorrelationRegistryStore, unique: str) -> tuple[str, str, str, str]:
    import psycopg

    src_id = f"imrg_src_{unique}"
    tgt_id = f"imrg_tgt_{unique}"
    eng_id = f"imrg_eng_{unique}"
    sugg_id = f"imrg_sugg_{unique}"
    with psycopg.connect(store.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO topinstal_identities (identity_id, primary_email, display_name) VALUES (%s,%s,%s)",
                (src_id, f"{unique}-src@example.invalid", "Regression Src"),
            )
            cur.execute(
                "INSERT INTO topinstal_identities (identity_id, primary_email, display_name) VALUES (%s,%s,%s)",
                (tgt_id, f"{unique}-tgt@example.invalid", "Regression Tgt"),
            )
            cur.execute(
                "INSERT INTO topinstal_engagements (engagement_id, identity_id, status) VALUES (%s,%s,'open')",
                (eng_id, src_id),
            )
            cur.execute(
                "INSERT INTO identity_binding_suggestions "
                "(suggestion_id, source_identity_id, target_identity_id, signal_type, confidence, status, evidence_json) "
                "VALUES (%s,%s,%s,'regression_signal',0.99,%s,'{}'::jsonb)",
                (sugg_id, src_id, tgt_id, SUGGESTION_APPROVED),
            )
        conn.commit()
    return src_id, tgt_id, eng_id, sugg_id


def _cleanup(store: PostgresCorrelationRegistryStore, src_id: str, tgt_id: str, eng_id: str, sugg_id: str) -> None:
    import psycopg

    with psycopg.connect(store.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM identity_merge_log WHERE source_identity_id=%s OR target_identity_id=%s",
                (src_id, tgt_id),
            )
            cur.execute("DELETE FROM identity_binding_suggestions WHERE suggestion_id=%s", (sugg_id,))
            cur.execute("DELETE FROM topinstal_engagements WHERE engagement_id=%s", (eng_id,))
            cur.execute("DELETE FROM topinstal_identities WHERE identity_id IN (%s,%s)", (src_id, tgt_id))
        conn.commit()


def test_execute_identity_merge_survives_cascade_delete_and_writes_durable_log() -> None:
    store = PostgresCorrelationRegistryStore(POSTGRES_TEST_DATABASE_URL)
    store.bootstrap()
    unique = uuid.uuid4().hex[:10]
    src_id, tgt_id, eng_id, sugg_id = _seed_approved_suggestion(store, unique)
    try:
        # RED (pre-fix): this call raised psycopg.errors.ForeignKeyViolation, because
        # delete_identity() cascade-deleted the suggestion row before write_identity_merge_log()
        # tried to INSERT a row referencing it. The engagement repoint and identity delete had
        # already committed independently by that point -- durable mutation, reported failure.
        result = execute_identity_merge(store, suggestion_id=sugg_id, operator_id="regression_operator")

        assert result["merged"] is True
        assert result["engagements_repointed"] == 1
        assert result["source_identity_id"] == src_id
        assert result["target_identity_id"] == tgt_id

        import psycopg

        with psycopg.connect(store.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM topinstal_identities WHERE identity_id=%s", (src_id,))
                assert cur.fetchone() is None, "source identity must be deleted"

                cur.execute("SELECT identity_id FROM topinstal_engagements WHERE engagement_id=%s", (eng_id,))
                row = cur.fetchone()
                assert row is not None and row[0] == tgt_id, "engagement must be repointed to target"

                cur.execute("SELECT 1 FROM identity_binding_suggestions WHERE suggestion_id=%s", (sugg_id,))
                assert cur.fetchone() is None, "suggestion row is expected to cascade-delete"

                cur.execute(
                    "SELECT operator_id, engagements_repointed, suggestion_id, status "
                    "FROM identity_merge_log WHERE source_identity_id=%s AND target_identity_id=%s",
                    (src_id, tgt_id),
                )
                log_row = cur.fetchone()
                assert log_row is not None, "identity_merge_log row must survive the cascade delete (durable audit)"
                assert log_row[0] == "regression_operator"
                assert log_row[1] == 1
                assert log_row[2] is None, "suggestion_id must be nulled by ON DELETE SET NULL, not block the insert"
                assert log_row[3] == "completed"

        # Retry: the suggestion is gone (cascade-deleted), so a second merge attempt must not
        # be able to run at all -- proving retry cannot perform a second merge.
        with pytest.raises(ValueError, match="not found"):
            execute_identity_merge(store, suggestion_id=sugg_id, operator_id="regression_operator")
    finally:
        _cleanup(store, src_id, tgt_id, eng_id, sugg_id)
