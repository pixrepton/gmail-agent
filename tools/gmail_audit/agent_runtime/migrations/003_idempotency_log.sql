-- PR-5B: Idempotent Writes — idempotency_log table
-- Każdy write executor sprawdza ten log przed wykonaniem.
-- Jeśli operacja z danym kluczem była już wykonana, zwracany jest poprzedni wynik.

CREATE TABLE IF NOT EXISTS idempotency_log (
    key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index dla szybkich zapytań po dacie (sprzątanie starych rekordów)
CREATE INDEX IF NOT EXISTS idx_idempotency_log_created_at
    ON idempotency_log (created_at);
