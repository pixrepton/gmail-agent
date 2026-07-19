"""Process snapshot (extracted from gmail_intake.py)."""

from __future__ import annotations

from typing import Any
from config import Settings


def process_snapshot(
    *,
    settings: Settings,
    schema: dict[str, Any],
    instructions: str,
    run_state: dict[str, Any],
    snapshot: dict[str, Any],
    model: str | None,
    verbose: bool,
    keep_going: bool,
) -> bool:
    # Lazy imports from gmail_intake so test mock.patch("gmail_intake.X") is
    # resolved at function-call time (after mocks are applied).
    from gmail_intake import (
        _build_gmail_triage_result,
        _build_lane_stage_plan,
        _build_spine_first_intake_validation_result,
        _build_stage_record,
        _persist_stage_record,
        _record_gmail_raw_observation,
        append_jsonl,
        build_context_bundle,
        build_validation_result,
        check_runtime_stop_conditions,
        ConfigError,
        daszek_legacy_v2_push_allowed,
        enrich_snapshot_for_inference,
        envelopes_for_telemetry,
        EventLog,
        GroqClientError,
        hydrate_intelligence_seam_config,
        is_payload_too_large_error_message,
        is_rate_limit_error_message,
        OUTPUT_ORIGIN_NORMALIZED_VALID,
        OUTPUT_ORIGIN_RAW_VALID,
        OUTPUT_ORIGIN_REPAIRED_VALID,
        preclassify_snapshot,
        push_preview_to_daszek,
        push_v2_projection_to_daszek,
        record_error,
        reset_structured_alternation_stage_slots_for_message,
        run_intake_reasoning,
        sanitize_for_storage,
        sanitize_text,
        validate_intake_output,
    )

    source_message = snapshot.get("source_message") or {}
    message_id = str(source_message.get("message_id") or "").strip()
    if not message_id:
        run_state["summary"]["items_failed"] += 1
        record_error(run_state, stage="snapshot", message_id="", error="Frozen source snapshot is missing source_message.message_id.")
        return keep_going

    reset_structured_alternation_stage_slots_for_message()
    context_bundle = build_context_bundle(snapshot)
    stage_config = {
        "settings": settings,
        "schema": schema,
        "instructions": instructions,
        "model": model,
        "verbose": verbose,
        "snapshot": snapshot,
    }
    raw_observation_result = _record_gmail_raw_observation(
        settings=settings,
        run_state=run_state,
        snapshot=snapshot,
    )
    if raw_observation_result.get("observation") is not None:
        stage_config["raw_observation"] = raw_observation_result["observation"]
    triage_result = _build_gmail_triage_result(stage_config["raw_observation"])
    stage_config["triage_result"] = triage_result
    append_jsonl(
        run_state["artifacts"]["triage_results"],
        {
            "message_id": message_id,
            "observation_id": str(stage_config["raw_observation"].observation_id),
            "source_kind": str(stage_config["raw_observation"].source_kind),
            "triage_class": triage_result.get("triage_class"),
            "routing_decision": triage_result.get("routing_decision"),
            "reasoning_budget": sanitize_for_storage(triage_result.get("reasoning_budget") or {}),
            "batching": sanitize_for_storage(triage_result.get("batching") or {}),
            "preclassification": sanitize_for_storage(triage_result.get("preclassification") or {}),
        },
    )
    hydrate_intelligence_seam_config(run_state, snapshot, stage_config)
    stage_config["event_log"] = EventLog()
    enrich_snapshot_for_inference(snapshot, stage_config)
    tel = envelopes_for_telemetry(snapshot)
    sa = run_state["summary"]["semantic_alignment"]
    if tel.get("first_pass_attachment_envelope"):
        sa["items_with_attachment_envelope"] += 1
    if tel.get("first_pass_thread_context_envelope"):
        sa["items_with_thread_context_envelope"] += 1
    sa["approx_enrichment_chars_sum"] += int(tel.get("approx_enrichment_json_chars") or 0)
    append_jsonl(run_state["source_messages_path"], snapshot)
    preclassification_result = sanitize_for_storage(triage_result.get("preclassification") or preclassify_snapshot(snapshot, stage_config))
    lane_stage_plan = _build_lane_stage_plan(preclassification_result)
    stage_config["preclassification_result"] = preclassification_result
    stage_config["lane_stage_plan"] = lane_stage_plan
    # Faza 1a: metryki per tor
    lane = str(preclassification_result.get("lane", "unknown"))
    run_state.setdefault("_lane_counts", {})
    run_state["_lane_counts"][lane] = run_state["_lane_counts"].get(lane, 0) + 1
    intake_llm_before_signal = bool(getattr(settings, "intake_llm_before_signal", False))

    # Zarejestruj też ścieżkę (spine-first vs llm-first)
    run_state["manifest"]["intake_path"] = "spine_first" if not intake_llm_before_signal else "llm_first"
    run_state["manifest"]["intake_llm_before_signal"] = intake_llm_before_signal
    run_state["manifest"]["spine_first_intake"] = not intake_llm_before_signal

    if not intake_llm_before_signal:
        validation_result = _build_spine_first_intake_validation_result(
            snapshot=snapshot,
            preclassification_result=preclassification_result,
            lane_stage_plan=lane_stage_plan,
        )
        intake_reasoning_result = validation_result["intake_reasoning_result"]
        inference_mode = str(
            intake_reasoning_result.get("request_meta", {}).get("final_inference_mode") or "spine_first_preclassified"
        )
        run_state["summary"]["items_processed"] += 1
        append_jsonl(
            run_state["intake_inputs_path"],
            {
                "message_id": message_id,
                "snapshot_version": snapshot.get("snapshot_version", ""),
                "preclassification_result": preclassification_result,
                "lane_stage_plan": lane_stage_plan,
                "default_inference_mode": inference_mode,
                "input_variants": [],
                "spine_first": True,
            },
        )
        append_jsonl(
            run_state["artifacts"]["execution_metadata"],
            {
                "message_id": message_id,
                "spine_first_intake": True,
                "intake_llm_before_signal": False,
            },
        )
    else:
        try:
            intake_reasoning_result = run_intake_reasoning(snapshot, context_bundle, stage_config)
            if intake_reasoning_result.get("second_pass_applied"):
                run_state["summary"]["semantic_alignment"]["second_pass_triggers"] += 1
        except GroqClientError as exc:
            run_state["summary"]["items_failed"] += 1
            category_override = "payload_too_large" if is_payload_too_large_error_message(str(exc)) else None
            if category_override is None and is_rate_limit_error_message(str(exc)):
                category_override = "throttle"
            record_error(
                run_state,
                stage="model",
                message_id=message_id,
                error=str(exc),
                details={"inference_trace": sanitize_for_storage(getattr(exc, "details", {}))},
                category_override=category_override,
            )
            _persist_stage_record(
                run_state,
                _build_stage_record(
                    message_id=message_id,
                    preclassification_result=preclassification_result,
                    lane_stage_plan=lane_stage_plan,
                    intake_reasoning_result={
                        "raw_output_text": "",
                        "response_json": {},
                        "execution_metadata": {"stage_name": "intake_reasoning", "error": sanitize_text(str(exc))},
                    },
                    intake_result_final=None,
                    case_link_result=None,
                    business_result=None,
                    reply_result=None,
                    action_plan_result=None,
                    case_intelligence_result=None,
                    mailbox_memory_result=None,
                    preview=None,
                    v2_projection=None,
                    review_decision={"required": True, "flags": [], "failure_stage": "model"},
                ),
            )
            if check_runtime_stop_conditions(run_state, stage="model", message_id=message_id):
                return False
            return keep_going

        append_jsonl(
            run_state["intake_inputs_path"],
            {
                "message_id": message_id,
                "snapshot_version": snapshot.get("snapshot_version", ""),
                "preclassification_result": preclassification_result,
                "lane_stage_plan": lane_stage_plan,
                "default_inference_mode": intake_reasoning_result.get("request_meta", {}).get("final_inference_mode", ""),
                "input_variants": intake_reasoning_result.get("input_variants") or [],
            },
        )
        append_jsonl(
            run_state["model_raw_path"],
            {
                "message_id": message_id,
                "inference_mode": intake_reasoning_result.get("request_meta", {}).get("final_inference_mode", ""),
                "request_meta": sanitize_for_storage(intake_reasoning_result.get("request_meta") or {}),
                "response_json": sanitize_for_storage(intake_reasoning_result.get("response_json") or {}),
                "output_text": sanitize_text(str(intake_reasoning_result.get("raw_output_text") or "")),
            },
        )

        validation_result = validate_intake_output(intake_reasoning_result, stage_config)
        run_state["summary"]["items_processed"] += 1
        validation_trace_llm = validation_result["validation_trace"]
        inference_mode = str(
            intake_reasoning_result.get("request_meta", {}).get("final_inference_mode")
            or "deterministic_preclassification"
        )
        if validation_trace_llm.normalized_candidate is not None:
            append_jsonl(
                run_state["model_normalized_path"],
                {
                    "message_id": message_id,
                    "inference_mode": inference_mode,
                    "notes": validation_trace_llm.normalization_notes,
                    "candidate": sanitize_for_storage(validation_trace_llm.normalized_candidate),
                },
            )
        if validation_trace_llm.repair_applied:
            append_jsonl(
                run_state["model_repair_path"],
                {
                    "message_id": message_id,
                    "inference_mode": inference_mode,
                    "status": "attempted",
                    "notes": validation_trace_llm.repair_notes,
                    "candidate": sanitize_for_storage(validation_trace_llm.repaired_candidate or {}),
                },
            )

    validation_trace = validation_result["validation_trace"]
    validation = validation_result["validation"]
    if intake_llm_before_signal:
        inference_mode = str(
            intake_reasoning_result.get("request_meta", {}).get("final_inference_mode")
            or "deterministic_preclassification"
        )

    if not validation_result["is_valid"] or validation_result["intake_output"] is None:
        if not validation.parse_ok:
            failure_category = "parse"
        elif not validation.schema_ok:
            failure_category = "schema"
        else:
            failure_category = "semantic"
        validation_errors = validation.errors
        if validation_result.get("guardrail_error"):
            validation_errors = [str(validation_result["guardrail_error"])]
            failure_category = "semantic"
        append_jsonl(
            run_state["validation_path"],
            build_validation_result(
                message_id=message_id,
                inference_mode=inference_mode,
                is_valid=validation.is_valid and not validation_result.get("guardrail_error"),
                parse_ok=validation.parse_ok,
                schema_ok=validation.schema_ok,
                semantic_ok=validation.semantic_ok and not validation_result.get("guardrail_error"),
                raw_valid=validation_result["raw_valid"],
                normalized_valid=validation_result["normalized_valid"],
                repaired_valid=validation_result["repaired_valid"],
                final_output_origin=validation_result["final_output_origin"],
                guardrail_applied=False,
                normalization_applied=validation_trace.normalization_applied,
                normalization_notes=validation_trace.normalization_notes,
                repair_applied=validation_trace.repair_applied,
                repair_notes=validation_trace.repair_notes,
                errors=validation_errors,
                original_action=validation_result["original_action"],
                final_action="",
                guardrail_flags=[],
            ),
        )
        if not validation.parse_ok:
            run_state["summary"]["items_invalid_json"] += 1
        elif not validation.schema_ok:
            run_state["summary"]["items_schema_invalid"] += 1
        else:
            run_state["summary"]["items_semantic_invalid"] += 1
        run_state["summary"]["items_failed"] += 1
        run_state["summary"]["validation_origin_distribution"][validation_result["final_output_origin"]] += 1
        record_error(
            run_state,
            stage="validation",
            message_id=message_id,
            error=f"Structured intake output failed {failure_category} validation.",
            details={
                "validation_errors": validation_errors,
                "raw_output_text": sanitize_text(str(intake_reasoning_result.get("raw_output_text") or "")),
                "inference_mode": inference_mode,
                "request_meta": sanitize_for_storage(intake_reasoning_result.get("request_meta") or {}),
                "normalization_notes": validation_trace.normalization_notes,
                "repair_notes": validation_trace.repair_notes,
            },
            category_override=failure_category,
        )
        _persist_stage_record(
            run_state,
            _build_stage_record(
                message_id=message_id,
                preclassification_result=preclassification_result,
                lane_stage_plan=lane_stage_plan,
                intake_reasoning_result=intake_reasoning_result,
                intake_result_final=None,
                case_link_result=None,
                business_result=None,
                reply_result=None,
                action_plan_result=None,
                case_intelligence_result=None,
                mailbox_memory_result=None,
                preview=None,
                v2_projection=None,
                review_decision={
                    "required": True,
                    "flags": validation_errors,
                    "final_output_origin": validation_result["final_output_origin"],
                    "failure_stage": "validation",
                },
            ),
        )
        if check_runtime_stop_conditions(run_state, stage="validation", message_id=message_id):
            return False
        return keep_going

    intake_output = validation_result["intake_output"]
    intake_result_final = validation_result["intake_result_final"]
    guardrail_flags = list(validation_result["guardrail_flags"])
    final_output_origin = str(validation_result["final_output_origin"])

    if guardrail_flags:
        run_state["summary"]["items_guardrail_downgraded"] += 1
    if validation_trace.final_output_origin == OUTPUT_ORIGIN_RAW_VALID:
        run_state["summary"]["items_raw_valid"] += 1
    elif validation_trace.final_output_origin == OUTPUT_ORIGIN_NORMALIZED_VALID:
        run_state["summary"]["items_normalized_valid"] += 1
    elif validation_trace.final_output_origin == OUTPUT_ORIGIN_REPAIRED_VALID:
        run_state["summary"]["items_repaired_valid"] += 1
    run_state["summary"]["validation_origin_distribution"][final_output_origin] += 1

    append_jsonl(
        run_state["validation_path"],
        build_validation_result(
            message_id=message_id,
            inference_mode=inference_mode,
            is_valid=True,
            parse_ok=validation.parse_ok,
            schema_ok=validation.schema_ok,
            semantic_ok=validation.semantic_ok,
            raw_valid=validation_result["raw_valid"],
            normalized_valid=validation_result["normalized_valid"],
            repaired_valid=validation_result["repaired_valid"],
            final_output_origin=final_output_origin,
            guardrail_applied=bool(guardrail_flags),
            normalization_applied=validation_trace.normalization_applied,
            normalization_notes=validation_trace.normalization_notes,
            repair_applied=validation_trace.repair_applied,
            repair_notes=validation_trace.repair_notes,
            errors=validation.errors,
            original_action=validation_result["original_action"],
            final_action=intake_output["decision"]["action"],
            guardrail_flags=guardrail_flags,
        ),
    )

    signal_runtime_mode = str(getattr(settings, "signal_runtime_mode", "active") or "active").strip().lower()
    signal_runtime_result = None
    if signal_runtime_mode == "active":
        from gmail_signal_adapter import run_gmail_signal_runtime

        signal_runtime_result = run_gmail_signal_runtime(
            settings=settings,
            run_state=run_state,
            snapshot=snapshot,
            intake_result_final=intake_result_final,
            preclassification_result=preclassification_result,
            lane_stage_plan=lane_stage_plan,
            context_bundle=context_bundle,
            raw_observation=stage_config.get("raw_observation"),
            triage_result=triage_result,
            model=model,
            verbose=verbose,
            dry_run=False,
        )
        signal_metadata = {
            "message_id": message_id,
            "signal_runtime_mode": signal_runtime_mode,
            "primary_signal_id": signal_runtime_result.primary_signal.signal_id,
            "signal_ids": [item.signal_id for item in signal_runtime_result.signals],
            "inserted_signal_count": sum(1 for item in signal_runtime_result.append_results if item.inserted),
            "reconcile_processing_state": (
                signal_runtime_result.reconcile_result.processing_state
                if signal_runtime_result.reconcile_result is not None
                else "not_run"
            ),
        }
        append_jsonl(run_state["artifacts"]["execution_metadata"], signal_metadata)
        if signal_runtime_result.reconcile_result is not None:
            reconcile_result = signal_runtime_result.reconcile_result
            stage_outputs = reconcile_result.stage_outputs or {}
            preview = reconcile_result.preview
            v2_projection = reconcile_result.v2_projection
            if reconcile_result.processing_state == "skipped_duplicate":
                append_jsonl(
                    run_state["stage_records_path"],
                    sanitize_for_storage(
                        {
                            "record_type": "signal_runtime_duplicate",
                            "message_id": message_id,
                            "signal_id": reconcile_result.signal_id,
                            "signal_kind": reconcile_result.signal_kind,
                            "source_kind": reconcile_result.source_kind,
                            "processing_state": reconcile_result.processing_state,
                            "review_decision": {
                                "required": bool(intake_result_final.get("review_required")),
                                "flags": intake_result_final.get("review_reasons") or [],
                                "final_output_origin": final_output_origin,
                                "guardrail_flags": guardrail_flags,
                                "signal_runtime": signal_metadata,
                            },
                        }
                    ),
                )
                append_jsonl(run_state["intake_outputs_path"], intake_output)
                if daszek_legacy_v2_push_allowed(settings, run_state) and reconcile_result.v2_projection:
                    _mb_dup = stage_outputs.get("mailbox_memory_result") or {}
                    _pol_dup = _mb_dup.get("policy_report") if isinstance(_mb_dup, dict) else None
                    push_v2_projection_to_daszek(
                        run_state=run_state,
                        message_id=message_id,
                        v2_projection=reconcile_result.v2_projection,
                        case_intelligence_result=stage_outputs.get("case_intelligence_result"),
                        event_log=None,
                        action_plan_result=stage_outputs.get("action_plan_result"),
                        intake_result_final=intake_result_final,
                        policy_report=_pol_dup if isinstance(_pol_dup, dict) else None,
                    )
                from daszek_v3_feed_runtime import maybe_push_operational_feed_after_reconcile

                maybe_push_operational_feed_after_reconcile(
                    run_state=run_state,
                    settings=settings,
                    reconcile_result=reconcile_result,
                    trigger_message_id=message_id,
                )
                run_state["summary"]["consecutive_failures"] = 0
                return True
            else:
                append_jsonl(run_state["intake_outputs_path"], intake_output)
                if preview is not None:
                    append_jsonl(run_state["dash_preview_path"], preview)
                _persist_stage_record(
                    run_state,
                    _build_stage_record(
                        message_id=message_id,
                        preclassification_result=preclassification_result,
                        lane_stage_plan=lane_stage_plan,
                        intake_reasoning_result=intake_reasoning_result,
                        intake_result_final=intake_result_final,
                        case_link_result=stage_outputs.get("case_link_result"),
                        business_result=stage_outputs.get("business_reasoning_result"),
                        reply_result=stage_outputs.get("reply_draft_result"),
                        action_plan_result=stage_outputs.get("action_plan_result"),
                        case_intelligence_result=stage_outputs.get("case_intelligence_result"),
                        mailbox_memory_result=stage_outputs.get("mailbox_memory_result"),
                        preview=preview,
                        v2_projection=v2_projection,
                        review_decision={
                            "required": bool(intake_result_final.get("review_required")),
                            "flags": intake_result_final.get("review_reasons") or [],
                            "final_output_origin": final_output_origin,
                            "guardrail_flags": guardrail_flags,
                            "signal_runtime": signal_metadata,
                        },
                    ),
                )
                run_state["summary"]["items_valid"] += 1
                run_state["summary"]["valid_outputs"].append(intake_output)
                run_state["summary"]["processed_message_ids"].append(message_id)
                run_state["summary"]["decision_distribution"][intake_output["decision"]["action"]] += 1
                for flag in intake_output["review"]["flags"]:
                    run_state["summary"]["review_flag_distribution"][flag] += 1
                _mb = stage_outputs.get("mailbox_memory_result") or {}
                _pol = _mb.get("policy_report") if isinstance(_mb, dict) else None
                if daszek_legacy_v2_push_allowed(settings, run_state) and v2_projection:
                    push_v2_projection_to_daszek(
                        run_state=run_state,
                        message_id=message_id,
                        v2_projection=v2_projection,
                        case_intelligence_result=stage_outputs.get("case_intelligence_result"),
                        event_log=None,
                        action_plan_result=stage_outputs.get("action_plan_result"),
                        intake_result_final=intake_result_final,
                        policy_report=_pol if isinstance(_pol, dict) else None,
                    )
                from daszek_v3_feed_runtime import maybe_push_operational_feed_after_reconcile

                maybe_push_operational_feed_after_reconcile(
                    run_state=run_state,
                    settings=settings,
                    reconcile_result=reconcile_result,
                    trigger_message_id=message_id,
                )
                if preview is not None:
                    should_continue = push_preview_to_daszek(
                        run_state=run_state,
                        preview=preview,
                        keep_going=keep_going,
                        action_plan_result=stage_outputs.get("action_plan_result"),
                        intake_result_final=intake_result_final,
                        policy_report=_pol if isinstance(_pol, dict) else None,
                    )
                    if not should_continue:
                        return False
                run_state["summary"]["consecutive_failures"] = 0
                return True

    raise ConfigError(
        "Gmail processing requires signal-active spine. "
        "Set SIGNAL_RUNTIME_MODE=active (default) and use signal-worker or signal-run for ingress. "
        "Legacy process_snapshot tail was removed (Epik 5 / CEL)."
    )
