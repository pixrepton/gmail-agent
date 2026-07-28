-- Agent runtime state store (PR-A). Apply via bootstrap_agent_runtime() or PostgresOperatorEngagementStore.bootstrap().
-- SoT for operator desk: operator_engagement_snapshots (distinct from mailbox_memory_snapshots).

CREATE TABLE IF NOT EXISTS operator_engagement_snapshots (
    engagement_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    version INT NOT NULL DEFAULT 1,
    snapshot_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    snapshot_diff JSONB,              -- PR-8J: diff-based storage (opcjonalny)
    last_trace_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',  -- aktywny, expired, materialized
    expired_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_operator_engagement_snapshots_case
    ON operator_engagement_snapshots(case_id);

-- PR-8J: Partial index — tylko aktywne snapshooty (pomija expired/materialized)
-- Plus ALTER dla bezpieczeństwa jeśli tabela istniała przed dodaniem kolumny status/snapshot_diff
ALTER TABLE operator_engagement_snapshots
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS snapshot_diff JSONB,
    ADD COLUMN IF NOT EXISTS expired_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_snapshots_active
    ON operator_engagement_snapshots(updated_at DESC)
    WHERE status NOT IN ('expired', 'materialized');

CREATE TABLE IF NOT EXISTS agent_runtime_turns (
    turn_id TEXT PRIMARY KEY,
    engagement_id TEXT NOT NULL,
    snapshot_version INT NOT NULL,  -- checklist alias: version
    tool_name TEXT NOT NULL DEFAULT '',
    tool_args_redacted JSONB NOT NULL DEFAULT '{}'::jsonb,
    plan_correlation JSONB NOT NULL DEFAULT '{}'::jsonb,
    tool_status TEXT NOT NULL DEFAULT '',
    turn_summary_pl TEXT NOT NULL DEFAULT '',
    tokens_used INT NOT NULL DEFAULT 0,
    trace_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_runtime_turns_engagement
    ON agent_runtime_turns(engagement_id, created_at);

-- Krok 8: Agent checkpoint (pozwala wznowic agenta po crashu)
ALTER TABLE operator_engagement_snapshots
    ADD COLUMN IF NOT EXISTS last_checkpoint_at TIMESTAMPTZ;
ALTER TABLE operator_engagement_snapshots
    ADD COLUMN IF NOT EXISTS checkpoint_turn INT DEFAULT 0;
