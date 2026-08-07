"""Append-only Correction Ledger (AI-OS 5.2) over divergence-loop tables.

Historical proposal/response rows are immutable. Learning candidate status may
transition (approve/reject) but rows are never deleted.
"""

from __future__ import annotations

import json
from typing import Any

from _protocols import DatabaseConnection


def fetch_correction_ledger(
    conn: DatabaseConnection,
    *,
    case_id: str = "",
    engagement_id: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
  """Join agent proposals with operator responses for audit trail."""
  clauses: list[str] = []
  params: list[Any] = []
  if case_id:
    clauses.append("p.case_id = %s")
    params.append(str(case_id))
  if engagement_id:
    clauses.append("p.engagement_id = %s")
    params.append(str(engagement_id))
  where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
  params.append(max(1, int(limit)))
  sql = f"""
    SELECT
      p.proposal_id,
      p.engagement_id,
      p.case_id,
      p.created_at AS proposed_at,
      p.proposal_type,
      p.proposal_content_json,
      p.proposal_reasoning_pl,
      p.source_pipeline,
      r.response_id,
      r.response_type,
      r.detected_at AS responded_at,
      r.detection_confidence,
      r.diff_summary_pl
    FROM agent_proposal_records p
    LEFT JOIN operator_response_records r ON r.proposal_id = p.proposal_id
    {where}
    ORDER BY p.created_at DESC
    LIMIT %s
  """
  with conn.cursor() as cur:
    cur.execute(sql, tuple(params))
    rows = cur.fetchall() or []
  out: list[dict[str, Any]] = []
  for row in rows:
    if isinstance(row, dict):
      item = dict(row)
    else:
      item = {
        "proposal_id": row[0],
        "engagement_id": row[1],
        "case_id": row[2],
        "proposed_at": str(row[3] or ""),
        "proposal_type": row[4],
        "proposal_content_json": row[5],
        "proposal_reasoning_pl": row[6],
        "source_pipeline": row[7],
        "response_id": row[8],
        "response_type": row[9],
        "responded_at": str(row[10] or "") if row[10] is not None else "",
        "detection_confidence": row[11],
        "diff_summary_pl": row[12],
      }
    content = item.get("proposal_content_json")
    if isinstance(content, str):
      try:
        item["proposal_content_json"] = json.loads(content)
      except json.JSONDecodeError:
        pass
    out.append(item)
  return out


__all__ = ["fetch_correction_ledger"]
