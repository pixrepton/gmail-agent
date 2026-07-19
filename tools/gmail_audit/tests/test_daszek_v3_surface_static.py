from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paths import DASZEK_ROOT, TOP_CODE_ROOT  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DASZEK = DASZEK_ROOT


def _daszek_api_surface() -> str:
    """Route registration (v3) + handlers + v2 routes (archive, legacy aliases)."""
    includes = DASZEK / "includes"
    parts = ("api-v3.php", "api-v3-handlers.php", "api-v2.php")
    return "\n".join((includes / name).read_text(encoding="utf-8") for name in parts)


def test_daszek_v3_routes_and_cockpit_view_are_registered() -> None:
    plugin = (DASZEK / "daszek.php").read_text(encoding="utf-8")
    api = _daszek_api_surface()
    app = (DASZEK / "public" / "app.js").read_text(encoding="utf-8")

    assert "daszek_api_register_v3_routes" in plugin
    assert "$namespace = 'daszek/v3'" in api
    assert "/cockpit" in api
    assert "/cohort-runs" in api
    assert "V3_API_BASE" in app
    index = (DASZEK / "public" / "index.php").read_text(encoding="utf-8")
    assert "data-view=\"desk\"" in index
    assert "data-view=\"day\"" in index
    assert "data-view=\"cases\"" in index
    assert "data-view=\"chat\"" in index
    assert "data-view=\"last_ingress\"" in index
    assert "data-view=\"cockpit\"" in index
    assert "data-view=\"quality\"" in index
    assert "data-view=\"cohort_runs\"" in index
    assert "data-view=\"archive\"" in index
    style = (DASZEK / "public" / "style.css").read_text(encoding="utf-8")
    assert "nav-group-label" in style or "nav-group-label" in index
    assert 'id="refresh-btn"' in index
    assert "bindClick" in app
    assert "sortCasesChronologically" in app
    assert "renderArchiveView" in app
    assert "archiveCaseById" in app
    assert "tryRestoreSession" in app
    assert "daszek_parse_request_path_fallback" in plugin
    assert "renderCockpitView" in app
    assert "renderCohortRunsView" in app
    assert "syncViewToUrl" in app
    assert "/cohort-runs" in app
    assert "renderLastIngressView" in app
    assert "renderSkrzatPanel" in app
    assert "data-skrzat-ask" in app
    assert "conversation_answer_envelope.v1" in app
    assert "/cases/(?P<id>[a-zA-Z0-9_:-]+)/skrzat/ask" in api
    assert "daszek_api_v3_skrzat_ask" in api
    assert "wp_remote_post" in api
    assert "DASZEK_NODE_B_API_BASE" in (DASZEK / "includes" / "config.php").read_text(encoding="utf-8")
    assert "resolveNavButtonViewId" in app
    assert "startLastIngressViewLoad" in app
    assert "normalizeMainViewId" in app
    assert "/ingress-quality-snapshots/latest" in api
    assert "/ingress-quality-snapshots" in api
    assert "daszek_api_v3_ingress_quality_snapshot_ingest" in api
    assert "daszek_check_ingress_quality_snapshot_write" in api
    assert "/operational-feed-snapshots/latest" in api
    assert "/operational-feed-snapshots" in api
    assert "daszek_api_v3_operational_feed_snapshot_ingest" in api
    assert "daszek_check_operational_feed_snapshot_write" in api
    assert "/agent-hitl/approve" in api
    assert "/agent-hitl/send" in api
    assert "daszek_api_v2_agent_hitl_approve" in api
    assert "daszek_api_v2_agent_hitl_send" in api
    assert "daszek_node_b_service_token" in api
    assert "daszek_check_node_b_service_token" in api
    assert "DASZEK_NODE_B_SERVICE_TOKEN" in (DASZEK / "includes" / "config.php").read_text(encoding="utf-8")
    assert "daszek_v3_normalize_operational_feed_schema_aliases" in api
    assert "feed['quality_readonly']" in api
    assert "/engagements/(?P<id>[a-zA-Z0-9_:-]+)/os-events" in api
    assert "daszek_api_v3_engagement_os_events" in api
    assert "/system/os-events/recent" in api
    assert "daszek_api_v3_system_os_events_recent" in api
    assert "loadOsEventsForEngagement" in app
    assert "renderSystemView" in app
    assert "startSystemViewLoad" in app
    assert "renderSystemObservabilityDashboard" in app
    assert "renderSystemHealthStrip" in app
    assert 'data-view="system"' in index
    assert "renderOsEventsSection" in app
    assert "refreshCaseDetailOsEvents" in app
    assert "Oś systemu" in app
    assert "os-event-list" in style or "os-event-list" in app
    assert "mermaid" in index.lower() or "mermaid.min.js" in index
    assert "renderSystemDiagramsSection" in app
    assert "initMermaidDiagrams" in app
    assert "system-diagram-card" in app or "system-diagram-card" in style
    assert "system-diagrams-manifest.js" in index
    assert "DASZEK_SYSTEM_DIAGRAMS_MANIFEST" in (DASZEK / "public" / "system-diagrams-manifest.js").read_text(encoding="utf-8")
    assert (TOP_CODE_ROOT / "knowledge" / "docs" / "daszek-system-diagrams.md").is_file()
    assert "<!-- @daszek-diagrams v1 -->" in (TOP_CODE_ROOT / "knowledge" / "docs" / "daszek-system-diagrams.md").read_text(encoding="utf-8")
    assert "system-mmd-kalk-top-pipeline" in (DASZEK / "public" / "system-diagrams-manifest.js").read_text(encoding="utf-8")
    assert "system-mmd-cieplo-pipeline" in (DASZEK / "public" / "system-diagrams-manifest.js").read_text(encoding="utf-8")
    assert "kt_path" in (DASZEK / "public" / "system-diagrams-manifest.js").read_text(encoding="utf-8")
    assert "operational_feed_snapshots.jsonl" in (DASZEK / "includes" / "store-v3.php").read_text(encoding="utf-8")
    assert "system_health_snapshots.jsonl" in (DASZEK / "includes" / "store-v3.php").read_text(encoding="utf-8")
    assert "/system-health-snapshots/latest" in api
    assert "daszek_api_v3_system_health_snapshot_ingest" in api
    assert "daszek_check_system_health_snapshot_write" in api
    assert "/operational-feed-snapshots/latest" in app
    assert "Brak zasilenia biurka z Node B" in app
    assert "Widok jest projekcją" in app
    assert "Biurko" in app and "Dzień operacyjny" in app and "Sprawy" in app and "Czat" in app and "Ostatni ingress" in app
    assert "__return_true" not in api
    assert "require_once" in plugin and "store-v3.php" in plugin
    assert "daszek_v3_bootstrap_storage" in (DASZEK / "includes" / "store-v3.php").read_text(encoding="utf-8")


def test_daszek_detail_routes_accept_runtime_safe_ids() -> None:
    api = _daszek_api_surface()

    assert "/cases/(?P<id>[a-zA-Z0-9_:-]+)" in api
    assert "/case-archive" in api
    assert "/cases/(?P<id>[a-zA-Z0-9_:-]+)/archive" in api
    assert "/cases/(?P<id>[a-zA-Z0-9_:-]+)/unarchive" in api
    assert "operator_case_archive" in (DASZEK / "includes" / "store-v2.php").read_text(encoding="utf-8")
    assert "/desk-notes/(?P<id>[a-zA-Z0-9_:-]+)" in api
    assert "/desk-notes/(?P<id>[a-zA-Z0-9_:-]+)/feedback" in api


def test_daszek_app_js_decision_view_copy_aligns_with_node_b_projection() -> None:
    """Static guard: Daszek remains projection-only; operator copy must not imply NBA/decision/execution."""
    app = (DASZEK / "public" / "app.js").read_text(encoding="utf-8")

    assert "collapsed_operator_pl" in app
    assert "details_collapsed_by_default" in app
    assert "action_type_label_pl" in app
    assert "situation_vs_decision_hint_pl" in app
    assert "expand_hint_pl" in app

    assert "Tryb rekomendowany" not in app
    assert "Rekomendowany następny krok" not in app
    assert "Co system proponuje" not in app

    assert "Zła sprawa" in app
    assert "data-note-action=\"zla_sprawa\"" in app

    assert "Wykonaj" not in app
    assert "wykonaj" not in app


def test_skrzat_panel_is_read_only_and_shows_evidence_sections() -> None:
    app = (DASZEK / "public" / "app.js").read_text(encoding="utf-8")
    api = _daszek_api_surface()

    assert "renderSkrzatPanel" in app
    assert "conversation_answer_envelope.v1" in app
    assert "context_audit" in app
    assert "query_text" in app
    assert "Kontekst LLM (audit)" in app
    assert "Dowody, braki i konflikty" in app
    assert "read_only" in app
    assert "action_allowed" in app
    assert "nie wykonuje akcji" in app.lower() or "nie wykonuje akcji" in app

    for forbidden in ("Wyślij maila", "execute_action", "autonomous send", "tryb Act", "mode_act"):
        assert forbidden not in app

    assert "daszek_api_v3_skrzat_ask" in api
    assert "wp_remote_post" in api
    assert "conversation_answer_envelope.v1" in api
    assert "node_b_unconfigured" in api
    assert "invalid_node_b_response" in api
    assert "build_case_context_pack" not in api
    skrzat_fn = api.split("function daszek_api_v3_skrzat_ask", 1)[-1].split("function ", 2)[0]
    assert "wp_remote_post" in skrzat_fn
    assert "build_case_context_pack" not in skrzat_fn
