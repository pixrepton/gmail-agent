"""AI-OS Roadmap 2.4 — HTTP API for operator feed-visibility override + exceptions_only preview."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.store import InMemoryOperatorEngagementStore  # noqa: E402
from api_app import create_app  # noqa: E402
from feed_visibility import (  # noqa: E402
    VISIBILITY_ATTENTION_REQUIRED,
    VISIBILITY_CASE_TIMELINE_ONLY,
    VISIBILITY_HIDDEN,
    VISIBILITY_MAIN_FEED,
    classify_signal_for_feed,
)
from llm_contracts.engagement_snapshot_v2 import (  # noqa: E402
    CaseUnderstandingStatusV1,
    EngagementSnapshotV2,
    FeedVisibility,
)
from tests.test_aios_2_4_x1_exceptions_only import _snapshot  # noqa: E402

_MUTATION_TOKEN_ENV_KEYS = ("DASZEK_NODE_B_API_TOKEN", "GMAIL_AGENT_INTERNAL_API_TOKEN", "NODE_B_REGISTRY_TOKEN")
_LEGAL_OVERRIDE_MODES = (VISIBILITY_HIDDEN, VISIBILITY_CASE_TIMELINE_ONLY, VISIBILITY_MAIN_FEED)


def _make_client() -> TestClient:
    return TestClient(
        create_app(runtime_provider=lambda: None, cohort_reader=lambda _r: None, registry_provider=lambda: None)
    )


def _auth_headers(token: str = "good-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _clear_tokens() -> None:
    for key in _MUTATION_TOKEN_ENV_KEYS:
        os.environ.pop(key, None)


def _seed_store(store: InMemoryOperatorEngagementStore, snap: EngagementSnapshotV2) -> None:
    store.insert_snapshot(snap)


def _main_feed_snapshot(**kwargs) -> EngagementSnapshotV2:
    visibility = FeedVisibility(**classify_signal_for_feed(preclassification_result={"lane": "intake_llm"}))
    return _snapshot(visibility=visibility, **kwargs)


def _overridden_hidden_snapshot(*, engagement_id: str) -> EngagementSnapshotV2:
    hidden = FeedVisibility(
        **__import__("feed_visibility").apply_operator_visibility_override(
            classify_signal_for_feed(preclassification_result={"lane": "intake_llm"}),
            mode=VISIBILITY_HIDDEN,
            reason="ack",
        )
    )
    return _snapshot(engagement_id=engagement_id, visibility=hidden)


def _assert_requested_vs_effective_fields(body: dict, *, requested: str | None) -> None:
    assert "requested_override_mode" in body
    assert "effective_feed_visibility_mode" in body
    assert "feed_visibility_mode" in body  # compatibility alias
    assert body["requested_override_mode"] == requested
    assert body["feed_visibility_mode"] == body["effective_feed_visibility_mode"]
    assert body["effective_feed_visibility_mode"] in {
        VISIBILITY_HIDDEN,
        VISIBILITY_CASE_TIMELINE_ONLY,
        VISIBILITY_MAIN_FEED,
        VISIBILITY_ATTENTION_REQUIRED,
    }
    if requested is not None:
        assert requested in _LEGAL_OVERRIDE_MODES
        assert requested != VISIBILITY_ATTENTION_REQUIRED


@pytest.fixture
def store() -> InMemoryOperatorEngagementStore:
    return InMemoryOperatorEngagementStore()


@pytest.fixture
def authed_env():
    _clear_tokens()
    os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
    yield
    _clear_tokens()


class TestFeedVisibilityOverrideApi:
    def test_requires_mutation_principal(self, store: InMemoryOperatorEngagementStore) -> None:
        _seed_store(store, _main_feed_snapshot(engagement_id="eng_api"))
        client = _make_client()
        _clear_tokens()
        with patch("operator_visibility_bridge.build_operator_engagement_store", return_value=store):
            response = client.post(
                "/engagements/eng_api/feed-visibility/override",
                json={"mode": "hidden"},
            )
        assert response.status_code == 401

    @pytest.mark.parametrize("mode", list(_LEGAL_OVERRIDE_MODES))
    def test_each_legal_override_mode(
        self, store: InMemoryOperatorEngagementStore, authed_env, mode: str
    ) -> None:
        eid = f"eng_mode_{mode}"
        _seed_store(store, _main_feed_snapshot(engagement_id=eid))
        client = _make_client()
        with patch("operator_visibility_bridge.build_operator_engagement_store", return_value=store):
            with patch("operator_visibility_bridge.best_effort_push_engagement_feed_after_hitl", return_value={"skipped": True}):
                with patch("operator_visibility_bridge.publish_os_event", return_value="evt-1"):
                    response = client.post(
                        f"/engagements/{eid}/feed-visibility/override",
                        json={"mode": mode, "reason": "param"},
                        headers=_auth_headers(),
                    )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        _assert_requested_vs_effective_fields(body, requested=mode)
        assert body["stored_feed_visibility_mode"] == mode
        assert body["feed_visibility"]["mode"] == mode
        assert body["feed_visibility"]["operator_override"] is True
        assert body["cleared"] is False

    def test_successful_override(self, store: InMemoryOperatorEngagementStore, authed_env) -> None:
        snap = _main_feed_snapshot(engagement_id="eng_ok", case_id="case_ok")
        _seed_store(store, snap)
        client = _make_client()
        with patch("operator_visibility_bridge.build_operator_engagement_store", return_value=store):
            with patch("operator_visibility_bridge.best_effort_push_engagement_feed_after_hitl", return_value={"skipped": True}):
                with patch("operator_visibility_bridge.publish_os_event", return_value="evt-1"):
                    response = client.post(
                        "/engagements/eng_ok/feed-visibility/override",
                        json={"mode": "hidden", "reason": "noise"},
                        headers=_auth_headers(),
                    )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["feed_visibility"]["mode"] == VISIBILITY_HIDDEN
        assert body["feed_visibility"]["operator_override"] is True
        _assert_requested_vs_effective_fields(body, requested=VISIBILITY_HIDDEN)
        saved = store.load_snapshot("eng_ok")
        assert saved is not None
        assert saved.feed_visibility is not None
        assert saved.feed_visibility.mode == VISIBILITY_HIDDEN

    def test_main_feed_request_can_yield_attention_required_effective(
        self, store: InMemoryOperatorEngagementStore, authed_env
    ) -> None:
        """requested main_feed ≠ effective attention_required — two different contracts."""
        snap = _main_feed_snapshot(engagement_id="eng_attn", hitl_required=True)
        hidden = FeedVisibility(
            **__import__("feed_visibility").apply_operator_visibility_override(
                classify_signal_for_feed(preclassification_result={"lane": "intake_llm"}),
                mode=VISIBILITY_HIDDEN,
                reason="prep",
            )
        )
        snap = snap.model_copy(update={"feed_visibility": hidden})
        _seed_store(store, snap)
        client = _make_client()
        with patch("operator_visibility_bridge.build_operator_engagement_store", return_value=store):
            with patch("operator_visibility_bridge.best_effort_push_engagement_feed_after_hitl", return_value={"skipped": True}):
                with patch("operator_visibility_bridge.publish_os_event", return_value="evt-1"):
                    response = client.post(
                        "/engagements/eng_attn/feed-visibility/override",
                        json={"mode": VISIBILITY_MAIN_FEED, "reason": "show"},
                        headers=_auth_headers(),
                    )
        assert response.status_code == 200
        body = response.json()
        assert body["requested_override_mode"] == VISIBILITY_MAIN_FEED
        assert body["stored_feed_visibility_mode"] == VISIBILITY_MAIN_FEED
        assert body["effective_feed_visibility_mode"] == VISIBILITY_ATTENTION_REQUIRED
        assert body["feed_visibility_mode"] == VISIBILITY_ATTENTION_REQUIRED  # alias = effective
        assert body["requested_override_mode"] != body["effective_feed_visibility_mode"]

    def test_idempotent_repeat_preserves_requested_and_effective(
        self, store: InMemoryOperatorEngagementStore, authed_env
    ) -> None:
        snap = _main_feed_snapshot(engagement_id="eng_idem")
        _seed_store(store, snap)
        client = _make_client()
        payload = {"mode": "hidden", "reason": "dup"}
        with patch("operator_visibility_bridge.build_operator_engagement_store", return_value=store):
            with patch("operator_visibility_bridge.best_effort_push_engagement_feed_after_hitl", return_value={"skipped": True}):
                with patch("operator_visibility_bridge.publish_os_event", return_value="evt-1"):
                    first = client.post(
                        "/engagements/eng_idem/feed-visibility/override",
                        json=payload,
                        headers=_auth_headers(),
                    )
                    version_after_first = store.load_snapshot("eng_idem").version
                    second = client.post(
                        "/engagements/eng_idem/feed-visibility/override",
                        json=payload,
                        headers=_auth_headers(),
                    )
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json().get("idempotent_replay") is True
        assert store.load_snapshot("eng_idem").version == version_after_first
        first_body = first.json()
        second_body = second.json()
        for key in (
            "requested_override_mode",
            "effective_feed_visibility_mode",
            "feed_visibility_mode",
            "stored_feed_visibility_mode",
        ):
            assert second_body[key] == first_body[key]
        _assert_requested_vs_effective_fields(second_body, requested=VISIBILITY_HIDDEN)

    def test_clear_without_mode(self, store: InMemoryOperatorEngagementStore, authed_env) -> None:
        _seed_store(store, _overridden_hidden_snapshot(engagement_id="eng_clear"))
        client = _make_client()
        with patch("operator_visibility_bridge.build_operator_engagement_store", return_value=store):
            with patch("operator_visibility_bridge.best_effort_push_engagement_feed_after_hitl", return_value={"skipped": True}):
                with patch("operator_visibility_bridge.publish_os_event", return_value="evt-1"):
                    response = client.post(
                        "/engagements/eng_clear/feed-visibility/override",
                        json={"clear": True},
                        headers=_auth_headers(),
                    )
        assert response.status_code == 200
        body = response.json()
        assert body["cleared"] is True
        assert body["requested_override_mode"] is None
        assert body["effective_feed_visibility_mode"] == body["feed_visibility_mode"]
        saved = store.load_snapshot("eng_clear")
        assert saved is not None
        assert saved.feed_visibility is not None
        assert saved.feed_visibility.operator_override is False
        assert saved.feed_visibility.mode == VISIBILITY_MAIN_FEED

    def test_clear_with_conflicting_mode_is_ambiguous_400(
        self, store: InMemoryOperatorEngagementStore, authed_env
    ) -> None:
        _seed_store(store, _overridden_hidden_snapshot(engagement_id="eng_amb"))
        client = _make_client()
        with patch("operator_visibility_bridge.build_operator_engagement_store", return_value=store):
            response = client.post(
                "/engagements/eng_amb/feed-visibility/override",
                json={"clear": True, "mode": "main_feed"},
                headers=_auth_headers(),
            )
        assert response.status_code == 400
        payload = response.json()
        blob = str(payload.get("detail") or payload.get("error") or payload).lower()
        assert "ambiguous" in blob
        saved = store.load_snapshot("eng_amb")
        assert saved is not None
        assert saved.feed_visibility is not None
        assert saved.feed_visibility.operator_override is True
        assert saved.feed_visibility.mode == VISIBILITY_HIDDEN

    def test_mode_required_when_not_clear(self, store: InMemoryOperatorEngagementStore, authed_env) -> None:
        _seed_store(store, _main_feed_snapshot(engagement_id="eng_nomode"))
        client = _make_client()
        with patch("operator_visibility_bridge.build_operator_engagement_store", return_value=store):
            response = client.post(
                "/engagements/eng_nomode/feed-visibility/override",
                json={},
                headers=_auth_headers(),
            )
        assert response.status_code == 400

    def test_invalid_mode_fail_closed(self, store: InMemoryOperatorEngagementStore, authed_env) -> None:
        _seed_store(store, _main_feed_snapshot(engagement_id="eng_bad"))
        client = _make_client()
        with patch("operator_visibility_bridge.build_operator_engagement_store", return_value=store):
            response = client.post(
                "/engagements/eng_bad/feed-visibility/override",
                json={"mode": "attention_required"},
                headers=_auth_headers(),
            )
        assert response.status_code == 400

    def test_missing_engagement(self, store: InMemoryOperatorEngagementStore, authed_env) -> None:
        client = _make_client()
        with patch("operator_visibility_bridge.build_operator_engagement_store", return_value=store):
            response = client.post(
                "/engagements/missing/feed-visibility/override",
                json={"mode": "hidden"},
                headers=_auth_headers(),
            )
        assert response.status_code == 404

    def test_override_does_not_change_understanding_or_readiness(
        self, store: InMemoryOperatorEngagementStore, authed_env
    ) -> None:
        understanding = CaseUnderstandingStatusV1(
            status="ok",
            reason="grounded",
        )
        snap = _main_feed_snapshot(engagement_id="eng_iso")
        snap = snap.model_copy(update={"case_understanding_status": understanding})
        _seed_store(store, snap)
        client = _make_client()
        with patch("operator_visibility_bridge.build_operator_engagement_store", return_value=store):
            with patch("operator_visibility_bridge.best_effort_push_engagement_feed_after_hitl", return_value={"skipped": True}):
                with patch("operator_visibility_bridge.publish_os_event", return_value="evt-1"):
                    response = client.post(
                        "/engagements/eng_iso/feed-visibility/override",
                        json={"mode": "case_timeline_only"},
                        headers=_auth_headers(),
                    )
        assert response.status_code == 200
        body = response.json()
        assert body.get("case_readiness_unchanged") is True
        saved = store.load_snapshot("eng_iso")
        assert saved is not None
        assert saved.case_understanding_status is not None
        assert saved.case_understanding_status.status == "ok"
        assert saved.operational_status.code == "enriching"
        assert body["case_understanding_status"]["status"] == "ok"


class TestOperationalFeedPreviewApi:
    def test_exceptions_only_false_keeps_full_desk(self, store: InMemoryOperatorEngagementStore) -> None:
        quiet = _main_feed_snapshot(engagement_id="quiet", status_code="enriching")
        hitl = _main_feed_snapshot(
            engagement_id="hitl",
            hitl_required=True,
            status_code="enriching",
        )
        _seed_store(store, quiet)
        _seed_store(store, hitl)
        client = _make_client()
        with patch("operator_visibility_bridge.build_operator_engagement_store", return_value=store):
            soft = client.get("/system/operational-feed", params={"exceptions_only": "false"})
            hard = client.get("/system/operational-feed", params={"exceptions_only": "true"})
        assert soft.status_code == 200
        assert hard.status_code == 200
        soft_ids = {row["engagement_id"] for row in soft.json()["snapshot"]["feed"]["desk"]}
        hard_ids = {row["engagement_id"] for row in hard.json()["snapshot"]["feed"]["desk"]}
        assert soft_ids == {"hitl", "quiet"}
        assert hard_ids == {"hitl"}

    def test_membership_filter_does_not_import_understanding_status(self, store: InMemoryOperatorEngagementStore) -> None:
        degraded = _main_feed_snapshot(engagement_id="deg", status_code="enriching")
        degraded = degraded.model_copy(
            update={
                "case_understanding_status": CaseUnderstandingStatusV1(
                    status="degraded",
                    reason="stale",
                )
            }
        )
        _seed_store(store, degraded)
        client = _make_client()
        with patch("operator_visibility_bridge.build_operator_engagement_store", return_value=store):
            response = client.get("/system/operational-feed", params={"exceptions_only": "true"})
        assert response.status_code == 200
        desk = response.json()["snapshot"]["feed"]["desk"]
        assert desk == []
