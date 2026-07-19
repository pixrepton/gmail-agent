"""Doctor command (extracted from gmail_intake.py)."""
from __future__ import annotations

import argparse


def run_doctor_command(args: argparse.Namespace) -> int:
    # Lazy imports from gmail_intake so test mock.patch("gmail_intake.X") is
    # resolved at function-call time (after mocks are applied).
    from gmail_intake import (
        _config_sources_subset,
        _emit_json,
        build_doctor_config_check,
        build_docling_check,
        build_google_auth_check,
        build_google_drive_check,
        build_neo4j_pilot_connectivity_check,
        build_ocr_check,
        build_otel_check,
        build_pgvector_check,
        build_unstructured_check,
        build_vector_retrieval_readiness_check,
        check_mailbox_memory_database,
        CHECK_STATUS_FAILED,
        CHECK_STATUS_OK,
        CHECK_STATUS_SKIPPED,
        ConfigError,
        DaszekClient,
        DaszekClientError,
        DOCTOR_STATUS_FAILED,
        DOCTOR_STATUS_FAILED_AUTH,
        DOCTOR_STATUS_FAILED_CONFIG,
        DOCTOR_STATUS_OK,
        empty_doctor_summary,
        existing_env_candidates,
        get_profile,
        GroqClientError,
        infer_mailbox,
        is_auth_error_message,
        load_settings,
        normalize_gmail_source,
        run_google_direct_auth_check,
        sanitize_text,
    )

    summary = empty_doctor_summary()
    local_envs = [str(path.resolve()) for path in existing_env_candidates()]
    if local_envs:
        summary["local_env_files"] = local_envs

    try:
        settings = load_settings(require_groq=True, require_google=not args.skip_gmail)
    except ConfigError as exc:
        summary["status"] = DOCTOR_STATUS_FAILED_CONFIG
        summary["checks"]["config"] = {"status": CHECK_STATUS_FAILED, "error": sanitize_text(str(exc))}
        _emit_json(summary)
        return 1

    summary["env_source"] = str(settings.env_path.resolve()) if settings.env_path else "environment_only"
    summary["checks"]["config"] = build_doctor_config_check(settings, model_override=args.model)
    summary["checks"]["otel"] = build_otel_check(settings)
    summary["checks"]["ocr"] = build_ocr_check(settings)
    summary["checks"]["pgvector"] = build_pgvector_check(settings)
    summary["checks"]["docling"] = build_docling_check(settings)
    summary["checks"]["unstructured"] = build_unstructured_check(settings)
    from agent_runtime.settings import load_agent_runtime_settings
    from agent_runtime.validate import build_agent_doctor_check

    agent_settings = load_agent_runtime_settings()
    summary["checks"]["agent_runtime"] = build_agent_doctor_check(agent_settings)
    if summary["checks"]["agent_runtime"].get("status") == "failed":
        summary["status"] = DOCTOR_STATUS_FAILED
    from agent_runtime.mcp_service import build_agent_mcp_doctor_check

    summary["checks"]["agent_runtime_mcp"] = build_agent_mcp_doctor_check()
    from daszek_engagement_feed import build_daszek_feed_doctor_check

    summary["checks"]["daszek_engagement_feed"] = build_daszek_feed_doctor_check(settings)
    if summary["checks"]["daszek_engagement_feed"].get("status") == "failed":
        summary["status"] = DOCTOR_STATUS_FAILED
    from agent_runtime.digital_twin_dod import build_digital_twin_doctor_check

    summary["checks"]["digital_twin_primary"] = build_digital_twin_doctor_check(settings)
    if summary["checks"]["digital_twin_primary"].get("status") == "failed":
        summary["status"] = DOCTOR_STATUS_FAILED
    summary["checks"]["neo4j_pilot"] = build_neo4j_pilot_connectivity_check(settings)
    drive_check = build_google_drive_check(settings, check_access=bool(args.check_drive))
    summary["checks"]["drive"] = drive_check
    from calendar_client import build_google_calendar_check

    calendar_check = build_google_calendar_check(settings, check_access=bool(getattr(args, "check_calendar", False)))
    summary["checks"]["calendar"] = calendar_check
    if summary["checks"]["otel"].get("status") == CHECK_STATUS_FAILED:
        summary["status"] = DOCTOR_STATUS_FAILED
    if summary["checks"]["pgvector"].get("status") == CHECK_STATUS_FAILED:
        summary["status"] = DOCTOR_STATUS_FAILED
    summary["checks"]["vector_retrieval"] = build_vector_retrieval_readiness_check(settings)
    if summary["checks"]["vector_retrieval"].get("status") == CHECK_STATUS_FAILED:
        summary["status"] = DOCTOR_STATUS_FAILED
    if bool(getattr(settings, "mailbox_memory_vector_enabled", False)):
        vr_doc = summary["checks"]["vector_retrieval"]
        if str(vr_doc.get("vector_path_status") or "") == "vector_path_unavailable":
            summary["warnings"].append(f"Vector retrieval unavailable: {str(vr_doc.get('reason') or '').strip()}")
    if summary["checks"]["docling"].get("status") == CHECK_STATUS_FAILED:
        summary["status"] = DOCTOR_STATUS_FAILED
    if summary["checks"]["unstructured"].get("status") == CHECK_STATUS_FAILED:
        summary["status"] = DOCTOR_STATUS_FAILED
    if summary["checks"]["neo4j_pilot"].get("status") == CHECK_STATUS_FAILED:
        summary["status"] = DOCTOR_STATUS_FAILED
    if drive_check.get("status") == CHECK_STATUS_FAILED:
        summary["status"] = DOCTOR_STATUS_FAILED
    if calendar_check.get("status") in {CHECK_STATUS_FAILED, "fail_env"}:
        summary["status"] = DOCTOR_STATUS_FAILED
    summary["checks"]["google_auth"] = build_google_auth_check(
        settings,
        require_google=not args.skip_gmail,
    )
    summary["gmail_source"] = normalize_gmail_source(args.gmail_source)

    if args.skip_gmail:
        summary["checks"]["google_direct"] = {"status": CHECK_STATUS_SKIPPED}
        summary["checks"]["gmail"] = {"status": CHECK_STATUS_SKIPPED}
    else:
        direct_check = run_google_direct_auth_check(settings)
        summary["checks"]["google_direct"] = direct_check
        summary["checks"]["google_auth"] = build_google_auth_check(
            settings,
            require_google=True,
        )
        if direct_check["status"] == CHECK_STATUS_FAILED:
            direct_error = str(direct_check.get("error") or "")
            summary["status"] = (
                DOCTOR_STATUS_FAILED_AUTH if is_auth_error_message(direct_error) else DOCTOR_STATUS_FAILED
            )
            summary["checks"]["gmail"] = {
                "status": CHECK_STATUS_SKIPPED,
                "reason": "Skipped because direct Google auth check already failed.",
            }
        else:
            try:
                profile = get_profile(
                    settings,
                    model=args.model,
                    verbose=args.verbose,
                    gmail_source=args.gmail_source,
                )
                summary["checks"]["google_auth"] = build_google_auth_check(
                    settings,
                    require_google=True,
                )
                summary["checks"]["gmail"] = {
                    "status": CHECK_STATUS_OK,
                    "mailbox": infer_mailbox(profile),
                    "source": normalize_gmail_source(args.gmail_source),
                }
            except GroqClientError as exc:
                summary["status"] = DOCTOR_STATUS_FAILED_AUTH if is_auth_error_message(str(exc)) else DOCTOR_STATUS_FAILED
                summary["checks"]["gmail"] = {
                    "status": CHECK_STATUS_FAILED,
                    "error": sanitize_text(str(exc)),
                    "source": normalize_gmail_source(args.gmail_source),
                }

    if args.check_daszek:
        summary["warnings"].append(
            "Daszek: legacy `task_count` from GET /daszek/v1/tasks is not the sole readiness KPI; "
            "also inspect daszek_v2_operator_surface and run projection proof reports after cohort runs."
        )
        try:
            client = DaszekClient(settings)
            client.login()
            tasks = client.list_tasks(refresh=True)
            legacy_note = "Compatibility seam only (v1 /tasks); operator UX is primarily v2 desk/cases/signals."
            v1_empty_note = (
                "v1_task_count=0 does not imply an empty Daszek; v2 ingest/projections and desk read APIs are separate."
            )
            summary["checks"]["daszek_v1_tasks"] = {
                "status": CHECK_STATUS_OK,
                "v1_task_count": len(tasks),
                "task_count": len(tasks),
                "base_url": client.base_url,
                "legacy_note": legacy_note,
                "v1_task_count_interpretation": v1_empty_note,
            }
            summary["checks"]["daszek"] = {
                "status": CHECK_STATUS_OK,
                "v1_task_count": len(tasks),
                "task_count": len(tasks),
                "base_url": client.base_url,
                "legacy_v1_tasks_note": legacy_note,
                "v1_task_count_interpretation": v1_empty_note,
            }

            v2_surface = {
                "status": CHECK_STATUS_OK,
                "daszek_v2_push_enabled": bool(settings.daszek_v2_push_enabled),
                "daszek_v2_readback_enabled": bool(settings.daszek_v2_readback_enabled),
                "daszek_v2_desk_relax_rejected": bool(settings.daszek_v2_desk_relax_rejected),
                "daszek_v2_desk_include_ignore": bool(settings.daszek_v2_desk_include_ignore),
                "daszek_v2_config_sources": _config_sources_subset(
                    settings,
                    "DASZEK_V2_PUSH",
                    "DASZEK_V2_READBACK_ENABLED",
                    "DASZEK_V2_DESK_RELAX_REJECTED",
                    "DASZEK_V2_DESK_INCLUDE_IGNORE",
                ),
                "ingest_reachable": "unknown",
            }
            try:
                profile = client.get_v2_calibration_profile()
                v2_surface["ingest_reachable"] = "ok"
                v2_surface["probe_endpoint"] = "calibration-profile"
                if isinstance(profile, dict):
                    v2_surface["probe_keys_sample"] = sorted(profile.keys())[:16]
            except DaszekClientError as exc:
                v2_surface["status"] = CHECK_STATUS_FAILED
                v2_surface["ingest_reachable"] = "failed"
                v2_surface["error"] = sanitize_text(str(exc))
                summary["status"] = DOCTOR_STATUS_FAILED_AUTH if is_auth_error_message(str(exc)) else DOCTOR_STATUS_FAILED

            if bool(getattr(args, "check_daszek_v2_read", False)):
                try:
                    desk = client.get_v2_desk()
                    v2_surface["desk_read_ok"] = isinstance(desk, dict)
                except DaszekClientError as exc:
                    v2_surface["desk_read_ok"] = False
                    v2_surface["desk_read_error"] = sanitize_text(str(exc))

            summary["checks"]["daszek_v2_operator_surface"] = v2_surface

            want_v3_feed_probe = bool(getattr(args, "check_daszek_v3_feed", False)) or bool(
                getattr(settings, "daszek_operational_feed_auto_push_enabled", False)
            )
            if want_v3_feed_probe:
                v3_feed_check: dict[str, object] = {
                    "endpoint": "/wp-json/daszek/v3/operational-feed-snapshots/latest",
                }
                try:
                    latest = client.get_v3_operational_feed_snapshot_latest()
                    snap = latest.get("snapshot") if isinstance(latest.get("snapshot"), dict) else None
                    v3_feed_check.update(
                        {
                            "status": CHECK_STATUS_OK,
                            "ok": latest.get("ok") is True,
                            "snapshot_present": snap is not None,
                            "latest_snapshot_id": str((snap or {}).get("snapshot_id") or ""),
                        }
                    )
                    feed = (snap or {}).get("feed") if isinstance((snap or {}).get("feed"), dict) else {}
                    if isinstance(feed, dict):
                        v3_feed_check["counts"] = {
                            "desk": len(feed.get("desk") or []),
                            "cases": len(feed.get("cases") or []),
                            "tasks": len(feed.get("tasks") or []),
                        }
                except DaszekClientError as exc:
                    v3_feed_check["status"] = CHECK_STATUS_FAILED
                    v3_feed_check["error"] = sanitize_text(str(exc))
                    summary["status"] = (
                        DOCTOR_STATUS_FAILED_AUTH if is_auth_error_message(str(exc)) else DOCTOR_STATUS_FAILED
                    )
                summary["checks"]["daszek_v3_operational_feed"] = v3_feed_check
            else:
                summary["checks"]["daszek_v3_operational_feed"] = {
                    "status": CHECK_STATUS_SKIPPED,
                    "reason": "Enable DASZEK_OPERATIONAL_FEED_AUTO_PUSH or pass --check-daszek-v3-feed.",
                }
        except DaszekClientError as exc:
            summary["status"] = DOCTOR_STATUS_FAILED_AUTH if is_auth_error_message(str(exc)) else DOCTOR_STATUS_FAILED
            summary["checks"]["daszek"] = {
                "status": CHECK_STATUS_FAILED,
                "error": sanitize_text(str(exc)),
            }
            summary["checks"]["daszek_v1_tasks"] = {"status": CHECK_STATUS_FAILED, "error": sanitize_text(str(exc))}
            summary["checks"]["daszek_v2_operator_surface"] = {"status": CHECK_STATUS_FAILED, "error": sanitize_text(str(exc))}
            summary["checks"]["daszek_v3_operational_feed"] = {"status": CHECK_STATUS_FAILED, "error": sanitize_text(str(exc))}
    else:
        summary["checks"]["daszek"] = {"status": CHECK_STATUS_SKIPPED}
        summary["checks"]["daszek_v1_tasks"] = {"status": CHECK_STATUS_SKIPPED}
        summary["checks"]["daszek_v2_operator_surface"] = {"status": CHECK_STATUS_SKIPPED}
        summary["checks"]["daszek_v3_operational_feed"] = {"status": CHECK_STATUS_SKIPPED}

    from case_snapshot_hot_state_contract import CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION

    summary["checks"]["case_snapshot_hot_state"] = {
        "status": CHECK_STATUS_OK,
        "schema_version": CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION,
        "daszek_preflight_correlated": bool(args.check_daszek),
    }

    db_url = str(getattr(settings, "mailbox_memory_database_url", "") or "").strip()
    if db_url:
        mm_check = check_mailbox_memory_database(db_url)
        summary["checks"]["mailbox_memory_database"] = mm_check
        if mm_check.get("status") == CHECK_STATUS_FAILED:
            summary["status"] = DOCTOR_STATUS_FAILED
    else:
        summary["checks"]["mailbox_memory_database"] = {
            "status": CHECK_STATUS_SKIPPED,
            "reason": "MAILBOX_MEMORY_DATABASE_URL not set.",
        }

    _emit_json(summary)
    return 0 if summary["status"] == DOCTOR_STATUS_OK else 1
