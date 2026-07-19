-- Learning loops DDL (Fale B + C). Applied via learning_loops_bootstrap_sql().

-- Fala B: operator-agent divergence loop
CREATE TABLE IF NOT EXISTS agent_proposal_records (
    proposal_id TEXT PRIMARY KEY,
    engagement_id TEXT NOT NULL DEFAULT '',
    case_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    proposal_type TEXT NOT NULL DEFAULT '',
    proposal_content_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    proposal_reasoning_pl TEXT NOT NULL DEFAULT '',
    source_pipeline TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_agent_proposal_records_case
    ON agent_proposal_records(case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_proposal_records_engagement
    ON agent_proposal_records(engagement_id, created_at DESC);

CREATE TABLE IF NOT EXISTS operator_response_records (
    response_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES agent_proposal_records(proposal_id) ON DELETE CASCADE,
    response_type TEXT NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    detection_confidence REAL NOT NULL DEFAULT 0.0,
    evidence_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    diff_summary_pl TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_operator_response_records_proposal
    ON operator_response_records(proposal_id);

CREATE TABLE IF NOT EXISTS learning_rule_candidates (
    candidate_id TEXT PRIMARY KEY,
    pattern_key TEXT NOT NULL,
    rule_text_pl TEXT NOT NULL DEFAULT '',
    supporting_count INT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending_operator',
    case_family TEXT NOT NULL DEFAULT '',
    proposal_type TEXT NOT NULL DEFAULT '',
    approved_at TIMESTAMPTZ,
    approved_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_learning_rule_candidates_status
    ON learning_rule_candidates(status, created_at DESC);

-- Fala C: historical corpus + world model (offline, separate from live cases)
CREATE TABLE IF NOT EXISTS historical_corpus_messages (
    corpus_message_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL DEFAULT 'export',
    source_ref TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body_text TEXT NOT NULL DEFAULT '',
    sender_email TEXT NOT NULL DEFAULT '',
    received_at TIMESTAMPTZ,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS historical_corpus_facts (
    corpus_fact_id TEXT PRIMARY KEY,
    corpus_message_id TEXT NOT NULL REFERENCES historical_corpus_messages(corpus_message_id) ON DELETE CASCADE,
    fact_key TEXT NOT NULL DEFAULT '',
    normalized_value TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS world_model_insights (
    insight_id TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT '',
    insight_text_pl TEXT NOT NULL DEFAULT '',
    supporting_count INT NOT NULL DEFAULT 0,
    source_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending_operator',
    approved_at TIMESTAMPTZ,
    approved_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_world_model_insights_status
    ON world_model_insights(status, category);
