-- Runs only on first cluster init (empty data volume) via docker-entrypoint-initdb.d.
CREATE EXTENSION IF NOT EXISTS vector;
