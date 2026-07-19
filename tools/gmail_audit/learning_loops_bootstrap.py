"""Bootstrap learning loops DDL (Fale B + C)."""

from __future__ import annotations

from pathlib import Path

LEARNING_LOOPS_MIGRATIONS_PATH = (
    Path(__file__).resolve().parent / "agent_runtime" / "LEARNING_LOOPS_MIGRATIONS.sql"
)


def learning_loops_bootstrap_sql() -> str:
    if not LEARNING_LOOPS_MIGRATIONS_PATH.is_file():
        return ""
    return LEARNING_LOOPS_MIGRATIONS_PATH.read_text(encoding="utf-8")


def bootstrap_learning_loops(conn) -> None:
    sql = learning_loops_bootstrap_sql()
    if not sql.strip():
        return
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
