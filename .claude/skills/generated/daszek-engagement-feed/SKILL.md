---
name: daszek-engagement-feed
description: "Skill for the Daszek_engagement_feed area of gmail-agent. 40 symbols across 11 files."
---

# Daszek_engagement_feed

40 symbols | 11 files | Cohesion: 60%

## When to Use

- Working with code in `tools/`
- Understanding how turns_from_snapshot_and_journal, what_changed_pl_from_snapshot, snapshot_to_feed_case work
- Modifying daszek_engagement_feed-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `tools/gmail_audit/daszek_engagement_feed/build.py` | turns_from_snapshot_and_journal, _utc_now_iso, _is_excluded_case, _snapshot_meta_key, build_feed_from_engagement_snapshots (+6) |
| `tools/gmail_audit/daszek_engagement_feed/case.py` | _strip, what_changed_pl_from_snapshot, _channel_meta, snapshot_to_feed_case, build_case_detail_from_engagement (+5) |
| `tools/gmail_audit/daszek_engagement_feed/day.py` | _operator_today_bounds, _parse_calendar_moment, _event_is_today, _calendar_items_for_case, _today_visit_items |
| `tools/gmail_audit/daszek_engagement_feed/labels.py` | case_kind_ui_meta, operational_status_label, primary_next_action_pl |
| `tools/gmail_audit/tests/test_engagement_feed_labels.py` | test_feed_case_maps_case_kind_to_family_label, test_primary_next_action_for_hitl_draft |
| `tools/gmail_audit/tests/test_daszek_engagement_feed_pr_e_complete.py` | test_build_engagement_feed_for_cel_with_extra_case, test_desk_item_carries_source_message_id |
| `tools/gmail_audit/daszek_engagement_feed/desk.py` | _desk_channel_meta, snapshot_to_desk_item |
| `tools/gmail_audit/tests/test_a1_case_understanding_projection.py` | test_active_feed_next_step_and_why_prefer_understanding_when_present, test_active_feed_why_on_desk_honest_empty_when_no_understanding |
| `tools/gmail_audit/agent_runtime/turn_journal.py` | list_turns |
| `tools/gmail_audit/daszek_engagement_feed/__init__.py` | build_engagement_feed_for_cel |

## Entry Points

Start here when exploring this area:

- **`turns_from_snapshot_and_journal`** (Function) — `tools/gmail_audit/daszek_engagement_feed/build.py:38`
- **`what_changed_pl_from_snapshot`** (Function) — `tools/gmail_audit/daszek_engagement_feed/case.py:73`
- **`snapshot_to_feed_case`** (Function) — `tools/gmail_audit/daszek_engagement_feed/case.py:111`
- **`build_case_detail_from_engagement`** (Function) — `tools/gmail_audit/daszek_engagement_feed/case.py:175`
- **`test_feed_case_maps_case_kind_to_family_label`** (Function) — `tools/gmail_audit/tests/test_engagement_feed_labels.py:16`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `turns_from_snapshot_and_journal` | Function | `tools/gmail_audit/daszek_engagement_feed/build.py` | 38 |
| `what_changed_pl_from_snapshot` | Function | `tools/gmail_audit/daszek_engagement_feed/case.py` | 73 |
| `snapshot_to_feed_case` | Function | `tools/gmail_audit/daszek_engagement_feed/case.py` | 111 |
| `build_case_detail_from_engagement` | Function | `tools/gmail_audit/daszek_engagement_feed/case.py` | 175 |
| `test_feed_case_maps_case_kind_to_family_label` | Function | `tools/gmail_audit/tests/test_engagement_feed_labels.py` | 16 |
| `build_engagement_feed_for_cel` | Function | `tools/gmail_audit/daszek_engagement_feed/__init__.py` | 87 |
| `build_feed_from_engagement_snapshots` | Function | `tools/gmail_audit/daszek_engagement_feed/build.py` | 193 |
| `build_engagement_feed_envelope` | Function | `tools/gmail_audit/daszek_engagement_feed/build.py` | 229 |
| `build_operational_feed_from_engagement_store` | Function | `tools/gmail_audit/daszek_engagement_feed/build.py` | 300 |
| `snapshot_to_feed_tasks` | Function | `tools/gmail_audit/daszek_engagement_feed/tasks.py` | 10 |
| `test_build_engagement_feed_for_cel_with_extra_case` | Function | `tools/gmail_audit/tests/test_daszek_engagement_feed_pr_e_complete.py` | 62 |
| `draft_reply_pl_from_snapshot` | Function | `tools/gmail_audit/daszek_engagement_feed/case.py` | 82 |
| `snapshot_to_desk_item` | Function | `tools/gmail_audit/daszek_engagement_feed/desk.py` | 50 |
| `case_kind_ui_meta` | Function | `tools/gmail_audit/daszek_engagement_feed/labels.py` | 29 |
| `operational_status_label` | Function | `tools/gmail_audit/daszek_engagement_feed/labels.py` | 34 |
| `test_desk_item_carries_source_message_id` | Function | `tools/gmail_audit/tests/test_daszek_engagement_feed_pr_e_complete.py` | 138 |
| `recommended_next_step_pl_from_snapshot` | Function | `tools/gmail_audit/daszek_engagement_feed/case.py` | 53 |
| `why_on_desk_pl_from_snapshot` | Function | `tools/gmail_audit/daszek_engagement_feed/case.py` | 63 |
| `primary_next_action_pl` | Function | `tools/gmail_audit/daszek_engagement_feed/labels.py` | 39 |
| `test_active_feed_next_step_and_why_prefer_understanding_when_present` | Function | `tools/gmail_audit/tests/test_a1_case_understanding_projection.py` | 213 |

## Connected Areas

| Area | Connections |
|------|-------------|
| Tests | 11 calls |
| Gmail_audit | 7 calls |
| Agent_runtime | 4 calls |

## How to Explore

1. `context({name: "turns_from_snapshot_and_journal"})` — see callers and callees
2. `query({search_query: "daszek_engagement_feed"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
