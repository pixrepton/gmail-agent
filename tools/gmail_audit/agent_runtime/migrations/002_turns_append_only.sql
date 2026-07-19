-- Append-only turns: explicit created_at on INSERT (no column default).
ALTER TABLE agent_runtime_turns ALTER COLUMN created_at DROP DEFAULT;
