## P0: Correlation Registry (Node B) — contract

### Purpose

- **Problem**: `mailbox_memory` (Gmail case, `case_id`) and Cieplo orchestrator workflows (`workflows.id`) are separate data models.
- **P0 contract**: a minimal **identity → engagement → links** registry in Node B Postgres plus a read-only `EngagementSnapshot` that can be fetched by `engagement_id`.

### Storage (Postgres, Node B)

Bootstrap is DDL-on-start (no migrations). Tables are created in the same Postgres as mailbox memory.

- **`topinstal_identities`**
  - `identity_id` (PK, text UUID)
  - `primary_email` (indexed on `lower(primary_email)`, **not unique** by design)
  - `display_name` (text, default `''`)
  - `metadata` (JSONB)
- **`topinstal_engagements`**
  - `engagement_id` (PK, text UUID)
  - `identity_id` (FK → identities)
  - `anchor_at` (used for recent-by-email window)
- **`correlation_links`**
  - Unique key: `(link_type, target_id, source_repo)` (idempotent upserts)
  - Points to `engagement_id`

### Canonical `link_type` values (P0)

Defined in `tools/gmail_audit/correlation_registry/link_types.py`:

- `mailbox_case` — `target_id = case_id` (source repo: `gmail-agent`)
- `gmail_message` — `target_id = Gmail API message id` (source repo: `gmail-agent` or `topinstal-cieplo-orchestrator`)
- `gmail_thread` — `target_id = Gmail thread id` (source repo: `gmail-agent`)
- `cieplo_workflow` — `target_id = workflows.id` (source repo: `topinstal-cieplo-orchestrator`)
- `canonical_trace` — `target_id = trace_id` (source repo: `topinstal-cieplo-orchestrator`)
- `cieplo_external_key` — `target_id = external_key` (source repo: `topinstal-cieplo-orchestrator`)
- `case_external_ref` — reserved for external case references
- `identity_email` — normalized email link (written by Node B on registration)

### Reserved `link_type` values (P1, not P0 write path)

Defined in `correlation_registry/link_types.py` as `LINK_TYPES_P1_RESERVED` (normalize rejects unknown types):

- `signal_journal_entry`, `workflow_event`, `action_proposal`, `event_spine_seq`
- `offer_snapshot`, `calc_request_snapshot`, `case_context_pack_ref`
- `merged_into`, `linked_case`

### Node B API (gmail-agent FastAPI)

Implementation: `tools/gmail_audit/api_app.py`.

- **Read**: `GET /cases/{case_id}/engagement`
  - Returns: `{ engagement_id, engagement, identity, links }`
- **Read**: `GET /engagements/{engagement_id}/snapshot`
  - Returns `EngagementSnapshot` (below)
- **Write (best-effort, internal)**: `POST /internal/registry/links`
  - Requires `Authorization: Bearer <NODE_B_REGISTRY_TOKEN>`

#### `POST /internal/registry/links` payload

Body is a JSON object:

- `identity_email` (string) — preferred identity email (may be empty if technical links exist)
- `display_name` (string, optional)
- `message_id` (string, optional) — if present, also registers `gmail_message` for Node B
- `within_days` (int, optional, default 30; clamped to 1..365)
- `links` (array of objects)
  - `link_type` (required, must be one of P0/P1-reserved link types)
  - `target_id` (required)
  - `source_repo` (optional; defaults to `gmail-agent`)
  - `confidence` (optional; number)
  - `metadata` (optional; object)

Notes:

- If `identity_email` is missing/empty, at least one technical link with `target_id` must exist.
- Registry resolution rules prefer **technical precedence** over email time-window (see `correlation_registry/heuristics.py`).

### `EngagementSnapshot` contract

Producer: Node B (`gmail-agent`), builder: `tools/gmail_audit/correlation_registry/snapshot.py`.

Top-level shape (schema `engagement_snapshot.v1`):

- `contract_name = "EngagementSnapshot"`
- `read_only = true`
- `schema_version = "engagement_snapshot.v1"`
- `engagement_id`
- `case_id` (best-effort from `mailbox_case` link)
- `cieplo_workflow_id` and `cieplo_workflow_ids` (from `cieplo_workflow` links)
- `identity` (row from `topinstal_identities`, or null/empty)
- `engagement` (row from `topinstal_engagements`)
- `correlation_links` (rows for this engagement)
- `case_context_pack` (optional; fetched from mailbox memory runtime)
- `workflow_context_pack` and `workflow_context_packs` (optional; fetched from orchestrator API)
- `missing_components` (list of `{component, reason, workflow_id?}`)
- `labels_pl` (UI labels)

### Workflow context-pack dependency (orchestrator)

`EngagementSnapshot` may fetch workflow packs in parallel over HTTP:

- Env: `CIEPLO_WORKFLOW_CONTEXT_BASE_URL`
- Route: `GET /internal/workflows/{workflow_id}/context-pack`
- Auth: `Authorization: Bearer <CIEPLO_WORKFLOW_CONTEXT_TOKEN>` (defaults to `NODE_B_REGISTRY_TOKEN` if unset)
