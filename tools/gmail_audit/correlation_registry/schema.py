"""SQL schema for P0 correlation registry (same Postgres as mailbox_memory)."""

from __future__ import annotations

CORRELATION_REGISTRY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS topinstal_identities (
    identity_id TEXT PRIMARY KEY,
    primary_email TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- No UNIQUE on primary_email: one mailbox may represent multiple investments (HVAC).
DROP INDEX IF EXISTS idx_topinstal_identities_primary_email;
CREATE INDEX IF NOT EXISTS idx_topinstal_identities_primary_email_lookup
    ON topinstal_identities (lower(primary_email));

CREATE TABLE IF NOT EXISTS topinstal_engagements (
    engagement_id TEXT PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES topinstal_identities(identity_id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'open',
    anchor_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_topinstal_engagements_identity_id
    ON topinstal_engagements (identity_id);
CREATE INDEX IF NOT EXISTS idx_topinstal_engagements_anchor_at
    ON topinstal_engagements (anchor_at DESC);

CREATE TABLE IF NOT EXISTS correlation_links (
    link_id TEXT PRIMARY KEY,
    engagement_id TEXT NOT NULL REFERENCES topinstal_engagements(engagement_id) ON DELETE CASCADE,
    link_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    source_repo TEXT NOT NULL DEFAULT 'gmail-agent',
    confidence REAL NOT NULL DEFAULT 1.0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_correlation_links_type_target_repo
    ON correlation_links (link_type, target_id, source_repo);
CREATE INDEX IF NOT EXISTS idx_correlation_links_engagement_id
    ON correlation_links (engagement_id);
CREATE INDEX IF NOT EXISTS idx_correlation_links_target
    ON correlation_links (link_type, target_id);
CREATE INDEX IF NOT EXISTS idx_correlation_links_updated_at
    ON correlation_links (updated_at DESC);

CREATE TABLE IF NOT EXISTS unified_os_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    engagement_id TEXT,
    source_repo TEXT NOT NULL DEFAULT 'gmail-agent',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_unified_os_events_engagement
    ON unified_os_events (engagement_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_unified_os_events_type
    ON unified_os_events (event_type, occurred_at DESC);

ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS processing_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS processor_id TEXT;
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS attempt_count INT NOT NULL DEFAULT 0;
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS failure_detail JSONB NOT NULL DEFAULT '{}'::jsonb;

-- P7 Observability: Event Spine trace-level fields (OpenTelemetry-compatible)
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS span_id TEXT;
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS parent_event_id TEXT;
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS case_id TEXT;
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS user_id TEXT;
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS severity TEXT NOT NULL DEFAULT 'info';
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS duration_ms INTEGER;
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS token_usage JSONB DEFAULT '{}'::jsonb;
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS cost NUMERIC(10,6);
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS success BOOLEAN;
ALTER TABLE unified_os_events
    ADD COLUMN IF NOT EXISTS error_message TEXT;

-- P7 Observability indexes
CREATE INDEX IF NOT EXISTS idx_os_events_trace_id
    ON unified_os_events (trace_id);
CREATE INDEX IF NOT EXISTS idx_os_events_case_id
    ON unified_os_events (case_id);
CREATE INDEX IF NOT EXISTS idx_os_events_severity
    ON unified_os_events (severity, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_os_events_session_id
    ON unified_os_events (session_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_unified_os_events_processing_poll
    ON unified_os_events (processing_status, occurred_at ASC)
    WHERE processing_status IN ('pending', 'failed');

CREATE TABLE IF NOT EXISTS event_spine_handler_effects (
    event_id TEXT NOT NULL,
    handler_key TEXT NOT NULL,
    effect_type TEXT NOT NULL DEFAULT 'audit',
    event_type TEXT NOT NULL DEFAULT '',
    engagement_id TEXT,
    processor_id TEXT,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (event_id, handler_key)
);
CREATE INDEX IF NOT EXISTS idx_event_spine_handler_effects_engagement
    ON event_spine_handler_effects (engagement_id, created_at DESC);

-- P2 Customer Identity: Poziom 2 suggest-only (RFC v1 §4)
CREATE TABLE IF NOT EXISTS identity_binding_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    source_identity_id TEXT NOT NULL REFERENCES topinstal_identities(identity_id) ON DELETE CASCADE,
    target_identity_id TEXT NOT NULL REFERENCES topinstal_identities(identity_id) ON DELETE CASCADE,
    signal_type TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'pending_operator',
    evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_identity_id, target_identity_id, signal_type)
);
CREATE INDEX IF NOT EXISTS idx_identity_binding_suggestions_status
    ON identity_binding_suggestions (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_identity_binding_suggestions_source
    ON identity_binding_suggestions (source_identity_id);

-- P2.0 Customer Identity metadata (RFC v1 §9 / plan Fala 4)
-- identity_kind: 'person' | 'organization' (additive, non-breaking)
-- property_anchor in topinstal_engagements.metadata (no schema change needed — stored in JSONB)
-- operator_merge_log: audit trail per merge operation
CREATE TABLE IF NOT EXISTS identity_merge_log (
    log_id TEXT PRIMARY KEY,
    suggestion_id TEXT REFERENCES identity_binding_suggestions(suggestion_id) ON DELETE SET NULL,
    source_identity_id TEXT NOT NULL,
    target_identity_id TEXT NOT NULL,
    operator_id TEXT NOT NULL DEFAULT 'system',
    engagements_repointed INT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'completed',
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_identity_merge_log_identities
    ON identity_merge_log (source_identity_id, target_identity_id);
"""

# ── Migracje schematu (Faza 4c) ──────────────────────────────────────────────
CORRELATION_REGISTRY_MIGRATIONS = [
    {
        "version": 1,
        "sql": """CREATE TABLE IF NOT EXISTS _schema_version (
            version INT PRIMARY KEY,
            applied_at TIMESTAMPTZ DEFAULT NOW()
        );""",
    },
    {
        "version": 2,
        "sql": "ALTER TABLE topinstal_identities ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'",
    },
]


from log_config import get_logger


def apply_correlation_registry_migrations(database_url: str) -> list[int]:
    """Apply unapplied migrations to the correlation registry schema.

    Returns a list of version numbers that were applied this call.
    """
    import psycopg

    applied: list[int] = []
    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                # Ensure _schema_version table exists (migration v1)
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS _schema_version ("
                    "  version INT PRIMARY KEY,"
                    "  applied_at TIMESTAMPTZ DEFAULT NOW()"
                    ")"
                )
                conn.commit()

                # Get already-applied versions
                cur.execute("SELECT version FROM _schema_version ORDER BY version")
                existing = {row[0] for row in cur.fetchall()}

                for migration in CORRELATION_REGISTRY_MIGRATIONS:
                    ver = migration["version"]
                    if ver in existing:
                        continue
                    with conn.cursor() as mig_cur:
                        mig_cur.execute(migration["sql"])
                        mig_cur.execute(
                            "INSERT INTO _schema_version (version, applied_at) VALUES (%s, NOW())",
                            (ver,),
                        )
                    applied.append(ver)

                if applied:
                    conn.commit()
    except Exception as exc:
        get_logger(__name__).warning(
            "correlation_registry_migrations failed: %s", exc
        )
    return applied
