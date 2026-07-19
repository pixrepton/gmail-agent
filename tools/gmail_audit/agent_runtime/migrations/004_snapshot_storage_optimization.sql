-- PR-8J: Snapshot Storage Optimization
--
-- 8.1: Partial index dla aktywnych snapshotów (pomija expired/materialized).
-- Przyspiesza zapytania list_recent_snapshots i list_staging_engagement_ids.
--
-- 8.2: Kolumna snapshot_diff do przechowywania diffów zamiast pełnych snapshotów.
-- (opcjonalne — wymaga refaktora store.py)

-- 8.1: Partial index — tylko aktywne snapshooty
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_snapshots_active
    ON operator_engagement_snapshots (updated_at DESC)
    WHERE status IS DISTINCT FROM 'expired'
      AND status IS DISTINCT FROM 'materialized';

-- 8.2: Dodanie kolumny dla diff-based storage
ALTER TABLE operator_engagement_snapshots
    ADD COLUMN IF NOT EXISTS snapshot_diff JSONB;

-- Komentarz dla przyszłego wdrożenia 8.2:
-- snapshot_diff przechowuje różnicę między obecnym a poprzednim snapshotem.
-- Pełny snapshot nadal w snapshot_data (dla szybkiego odczytu).
-- diff jest używany do audytu i replay'u.
