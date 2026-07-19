#!/usr/bin/env python3
"""Pick Gate B row3 cohort message ids from mailbox_memory (exported-emails-v2 pool)."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

_GMAIL_AUDIT = Path(__file__).resolve().parent.parent / "tools" / "gmail_audit"
if str(_GMAIL_AUDIT) not in sys.path:
    sys.path.insert(0, str(_GMAIL_AUDIT))

from mail_classification import classify_message  # noqa: E402

GOOD_TYPES = frozenset({"lead_oferta", "serwis", "reklamacja_gwarancja", "dofinansowanie"})
SKIP_SUBJECT = re.compile(
    r"postgres|bootstrap|replace-path|newsletter|kampania|panasonic|hisense|webinar|wiert|"
    r"promocj|najtaniej|klimatyzator|ogrzewanie podłogowe",
    re.I,
)


def pick_cohort(*, limit: int = 10, seed: int = 20260710) -> dict:
    import psycopg

    db_url = os.environ.get(
        "MAILBOX_MEMORY_DATABASE_URL",
        "postgresql://mailbox_memory:memorka@mailbox-memory-db:5432/mailbox_memory",
    )
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT message_id, subject, snippet, sender, sender_email, labels, body_text, received_at
                FROM mailbox_memory_messages
                ORDER BY received_at DESC NULLS LAST
                """
            )
            rows = cur.fetchall()

    candidates: list[dict] = []
    for mid, subject, snippet, sender, sender_email, labels_raw, body_text, received_at in rows:
        subject_s = subject or ""
        if SKIP_SUBJECT.search(subject_s):
            continue
        labels = json.loads(labels_raw) if isinstance(labels_raw, str) else (labels_raw or [])
        cls = classify_message(
            subject=subject_s,
            snippet=snippet or "",
            sender=(sender or sender_email or ""),
            labels=labels,
            body=body_text or "",
            has_attachment=False,
            direction="inbound",
        )
        if cls.get("case_type") not in GOOD_TYPES or not cls.get("candidate"):
            continue
        candidates.append(
            {
                "message_id": str(mid),
                "case_type": cls["case_type"],
                "priority": cls.get("priority_label"),
                "subject": subject_s[:120],
                "received_at": received_at.isoformat() if received_at else "",
            }
        )

    rng = random.Random(seed)
    pool = list(candidates)
    rng.shuffle(pool)
    picked = pool[: max(1, int(limit))]
    return {
        "source": "mailbox_memory_messages+mail_classification",
        "seed": seed,
        "pool_size": len(candidates),
        "picked_count": len(picked),
        "message_ids": [row["message_id"] for row in picked],
        "picked": picked,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260710)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    payload = pick_cohort(limit=args.limit, seed=args.seed)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(str(args.output.resolve()))
    else:
        print(text, end="")
    return 0 if payload["picked_count"] >= 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
