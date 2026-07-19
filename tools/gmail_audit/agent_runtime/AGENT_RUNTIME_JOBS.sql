-- Optional agent job audit queue (PR-D). Inline execution by default; rows record completed runs.

CREATE TABLE IF NOT EXISTS agent_runtime_jobs (
    job_id TEXT PRIMARY KEY,
    engagement_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    case_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error_text TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_runtime_jobs_engagement
    ON agent_runtime_jobs(engagement_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_runtime_jobs_status
    ON agent_runtime_jobs(status, created_at DESC);
