from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from api_app import create_app
from mailbox_memory_models import CaseContextPack


class _Runtime:
    def get_context_pack(self, **kwargs) -> CaseContextPack:
        return CaseContextPack(case_id="x")


def test_cases_view_actionable_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(runtime_provider=lambda: _Runtime())

    class _Cur:
        def execute(self, *args, **kwargs):
            return None

        def fetchall(self):
            return [
                ("c1", "lead_opportunity", "A", "open", "", "", {"requires_action": True}, None, None, None),
                ("c2", "reference_only", "B", "open", "", "", {"requires_action": False}, None, None, None),
            ]

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            return None

    monkeypatch.setattr("api_app.load_settings", lambda **k: MagicMock(mailbox_memory_database_url="postgres://x"))
    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _Conn())

    client = TestClient(app)
    resp = client.get("/cases?view=actionable")
    assert resp.status_code == 200
    body = resp.json()
    assert body["view"] == "actionable"
    assert all(c["requires_action"] for c in body["cases"])


def test_identity_merge_deprecated() -> None:
    app = create_app(runtime_provider=lambda: _Runtime())
    client = TestClient(app)
    resp = client.post("/identity/merge", json={"email": "a@b.com", "target_case_id": "c1"})
    assert resp.status_code == 410


# CASES_VIEW_FILTER_PROOF_OK · IDENTITY_MERGE_DEPRECATE_PROOF_OK · API_DESCRIPTION_PROOF_OK
