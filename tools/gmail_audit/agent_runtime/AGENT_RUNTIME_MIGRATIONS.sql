-- Idempotent migrations for agent runtime + signal spine linkage (PR-A).

ALTER TABLE mailbox_memory_signals
    ADD COLUMN IF NOT EXISTS engagement_id TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_mailbox_memory_signals_engagement_id
    ON mailbox_memory_signals(engagement_id)
    WHERE engagement_id <> '';

-- Backfill column on agent_runtime_turns when upgrading from early PR-A DDL.
ALTER TABLE agent_runtime_turns
    ADD COLUMN IF NOT EXISTS turn_summary_pl TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_runtime_turns
    ADD COLUMN IF NOT EXISTS tokens_used INT NOT NULL DEFAULT 0;

-- Rename legacy column `version` -> `snapshot_version` when present (no-op if already migrated).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'agent_runtime_turns' AND column_name = 'version'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'agent_runtime_turns' AND column_name = 'snapshot_version'
    ) THEN
        ALTER TABLE agent_runtime_turns RENAME COLUMN version TO snapshot_version;
    END IF;
END $$;

-- Krok 8: Agent checkpoint kolumny
ALTER TABLE operator_engagement_snapshots
    ADD COLUMN IF NOT EXISTS last_checkpoint_at TIMESTAMPTZ;
ALTER TABLE operator_engagement_snapshots
    ADD COLUMN IF NOT EXISTS checkpoint_turn INT DEFAULT 0;


-- MAX-STACK W2: AgentRun checkpoints (RFC agent-run-checkpoint-v1)
CREATE TABLE IF NOT EXISTS agent_run_checkpoints (
    run_id TEXT NOT NULL,
    engagement_id TEXT NOT NULL,
    turn_idx INT NOT NULL,
    snapshot_json JSONB NOT NULL,
    planner_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'running',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, turn_idx)
);
CREATE INDEX IF NOT EXISTS idx_agent_run_checkpoints_engagement
    ON agent_run_checkpoints(engagement_id, created_at DESC);
