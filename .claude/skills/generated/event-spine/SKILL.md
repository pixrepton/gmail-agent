---
name: event-spine
description: "Skill for the Event_spine area of gmail-agent. 50 symbols across 20 files."
---

# Event_spine

50 symbols | 20 files | Cohesion: 70%

## When to Use

- Working with code in `tools/`
- Understanding how engagement_timeline, fetch_merged_engagement_timeline, publish_os_event work
- Modifying event_spine-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `tools/gmail_audit/event_spine/health_monitor.py` | _detect_risk_stale_engagements, _detect_risk_blocked_hitl, _detect_risk_overdue_sla, evaluate_deterministic_risk_flags, _parse_heartbeat (+1) |
| `tools/gmail_audit/event_spine/store.py` | _parse_json_mapping, _row_to_event, claim_batch, _tuple_from_row, claim_batch (+1) |
| `tools/gmail_audit/event_spine/timeline.py` | _parse_ts, _case_event_rows, _os_event_rows, _agent_turn_rows, fetch_merged_engagement_timeline |
| `tools/gmail_audit/event_spine/gmail_telemetry.py` | _database_url, publish_gmail_feed_push_event, _base_payload, publish_gmail_reconcile_completed |
| `tools/gmail_audit/tests/test_gmail_os_event_telemetry.py` | test_publish_gmail_feed_push_event_success, test_publish_gmail_feed_push_event_failure, test_publish_gmail_reconcile_completed_skips_duplicate, test_publish_gmail_reconcile_completed_emits |
| `tools/gmail_audit/event_spine/emitter.py` | _new_event_id, publish_os_event, _execute_os_event_insert |
| `tools/gmail_audit/tests/test_health_monitor.py` | test_detect_risk_stale_engagements_skips_internal_task, test_detect_risk_blocked_hitl_flags_old_pending, test_build_health_status_includes_risk_flags |
| `tools/gmail_audit/agent_runtime/turn_journal.py` | list_turns, _connect |
| `tools/gmail_audit/api_app.py` | engagement_timeline, engagement_os_events |
| `tools/gmail_audit/agent_runtime/materialize.py` | _emit_os_event, _execute_composite_step |

## Entry Points

Start here when exploring this area:

- **`engagement_timeline`** (Function) — `tools/gmail_audit/api_app.py:681`
- **`fetch_merged_engagement_timeline`** (Function) — `tools/gmail_audit/event_spine/timeline.py:73`
- **`publish_os_event`** (Function) — `tools/gmail_audit/event_spine/emitter.py:17`
- **`test_publish_os_event_requires_database_url`** (Function) — `tools/gmail_audit/tests/test_event_spine_emitter.py:15`
- **`test_publish_os_event_requires_database_url`** (Function) — `tools/gmail_audit/tests/test_event_spine_processor.py:147`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `engagement_timeline` | Function | `tools/gmail_audit/api_app.py` | 681 |
| `fetch_merged_engagement_timeline` | Function | `tools/gmail_audit/event_spine/timeline.py` | 73 |
| `publish_os_event` | Function | `tools/gmail_audit/event_spine/emitter.py` | 17 |
| `test_publish_os_event_requires_database_url` | Function | `tools/gmail_audit/tests/test_event_spine_emitter.py` | 15 |
| `test_publish_os_event_requires_database_url` | Function | `tools/gmail_audit/tests/test_event_spine_processor.py` | 147 |
| `test_composite_plan_propagates_case_id_to_follow_up_step` | Function | `tools/gmail_audit/tests/test_materialize_composite.py` | 17 |
| `evaluate_deterministic_risk_flags` | Function | `tools/gmail_audit/event_spine/health_monitor.py` | 155 |
| `test_detect_risk_stale_engagements_skips_internal_task` | Function | `tools/gmail_audit/tests/test_health_monitor.py` | 18 |
| `test_detect_risk_blocked_hitl_flags_old_pending` | Function | `tools/gmail_audit/tests/test_health_monitor.py` | 41 |
| `engagement_os_events` | Function | `tools/gmail_audit/api_app.py` | 710 |
| `event_to_api_item` | Function | `tools/gmail_audit/event_spine/query.py` | 27 |
| `fetch_os_events_for_engagement` | Function | `tools/gmail_audit/event_spine/query.py` | 64 |
| `test_event_to_api_item_keeps_trace_as_correlation_and_case_as_entity_ref` | Function | `tools/gmail_audit/tests/test_os_event_w0.py` | 69 |
| `test_fetch_os_events_for_engagement_integration` | Function | `tools/gmail_audit/tests/test_os_event_w0.py` | 96 |
| `get_system_health_snapshot` | Function | `tools/gmail_audit/agent_runtime/business_pulse.py` | 488 |
| `build_health_status` | Function | `tools/gmail_audit/event_spine/health_monitor.py` | 188 |
| `test_build_health_status_includes_risk_flags` | Function | `tools/gmail_audit/tests/test_health_monitor.py` | 56 |
| `publish_gmail_feed_push_event` | Function | `tools/gmail_audit/event_spine/gmail_telemetry.py` | 30 |
| `test_publish_gmail_feed_push_event_success` | Function | `tools/gmail_audit/tests/test_gmail_os_event_telemetry.py` | 18 |
| `test_publish_gmail_feed_push_event_failure` | Function | `tools/gmail_audit/tests/test_gmail_os_event_telemetry.py` | 39 |

## Execution Flows

| Flow | Type | Steps |
|------|------|-------|
| `Get_system_health_snapshot → Case_family_value` | cross_community | 7 |
| `System_health_status → _parse_json_mapping` | cross_community | 5 |
| `System_briefing → _parse_json_mapping` | cross_community | 5 |
| `Get_system_health_snapshot → _parse_json_mapping` | cross_community | 5 |
| `Get_system_health_snapshot → _detect_risk_blocked_hitl` | cross_community | 4 |
| `Get_system_health_snapshot → _detect_risk_overdue_sla` | cross_community | 4 |

## Connected Areas

| Area | Connections |
|------|-------------|
| Gmail_audit | 5 calls |
| Agent_runtime | 2 calls |
| Mailbox_memory | 1 calls |
| Tests | 1 calls |

## How to Explore

1. `context({name: "engagement_timeline"})` — see callers and callees
2. `query({search_query: "event_spine"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
