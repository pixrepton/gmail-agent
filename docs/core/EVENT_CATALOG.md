# Event Catalog

**Weryfikacja:** 2026-07-13 (katalog emiterów + proof replay/idempotency decyzji)

Katalog eventów emitowanych przez pipeline Gmail-Agent do tabeli `unified_os_events`
przez `event_spine/emitter.py` → `publish_os_event(...)`.

> **Uwaga o nazewnictwie:** kod używa dwóch konwencji `event_type` obok siebie —
> kropkowanej przestrzeni nazw (np. `gmail.feed.pushed`, `agent.run.started`) oraz
> `snake_case` (np. `signal_received`, `message_received`, `action_proposal_created`).
> Poniższa lista odzwierciedla stan faktyczny w kodzie, nie docelową normalizację.

## Gmail / HITL / feed

| event_type                  | Emiter (`tools/gmail_audit/…`)   | Kiedy                                                                      |
| --------------------------- | -------------------------------- | -------------------------------------------------------------------------- |
| `gmail.feed.pushed`         | `event_spine/gmail_telemetry.py` | Operational feed wypchnięty do Daszek v3 (`publish_gmail_feed_push_event`) |
| `gmail.feed.push_failed`    | `event_spine/gmail_telemetry.py` | Push feedu do Daszka się nie powiódł                                       |
| `gmail.reconcile.completed` | `event_spine/gmail_telemetry.py` | Reconcile zakończony (pomija `skipped_duplicate`)                          |
| `gmail.hitl.approved`       | `agent_hitl_bridge.py`           | Operator zatwierdził akcję HITL                                            |

## Agent runtime

| event_type                     | Emiter (`tools/gmail_audit/…`)        | Kiedy                                                                                                   |
| ------------------------------ | ------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `agent.run.started`            | `agent_runtime/run.py`                | Start przebiegu agenta                                                                                  |
| `agent.run.completed`          | `agent_runtime/run.py`                | Koniec przebiegu agenta                                                                                 |
| `agent.tool.invoked`           | `agent_runtime/graph.py`              | Po wywołaniu narzędzia przez agenta                                                                     |
| `agent.write.{operation}`      | `agent_runtime/materialize.py`        | Materializacja zapisu (np. `agent.write.create_case`) — `event_type` budowany dynamicznie z `operation` |
| `case_os.materialize.approved` | `agent_runtime/materialize_bridge.py` | Operator zatwierdził materializację                                                                     |

## Mailbox memory (`mailbox_memory_runtime.py`)

| event_type                                | Kiedy                                        |
| ----------------------------------------- | -------------------------------------------- |
| `message_received`                        | Wiadomość zapisana do mailbox memory         |
| `case_linked`                             | Wiadomość powiązana ze sprawą                |
| `case_snapshot_updated`                   | Zaktualizowano snapshot sprawy               |
| `facts_extracted`                         | Wyekstrahowano fakty                         |
| `attachment_parsed`                       | Sparsowano załącznik                         |
| `next_action_updated`                     | Zmieniono next action sprawy                 |
| `document_intelligence_refresh_completed` | Odświeżenie document intelligence zakończone |
| `document_intelligence_failed`            | Błąd document intelligence                   |

## Event memory (`event_memory.py`)

| event_type                    | Emiter                   | Kiedy                                |
| ----------------------------- | ------------------------ | ------------------------------------ |
| `signal_received`             | `emit_signal_received`   | Odebrano sygnał                      |
| `case_intelligence_generated` | `emit_case_intelligence` | Wygenerowano case intelligence       |
| `feedback_recorded`           | `emit_feedback_event`    | Zapisano feedback operatora          |
| (dynamiczny)                  | `emit_desk_note_event`   | `event_type` z argumentu (desk note) |

## Drive ingest (`drive_ingest_runtime.py`)

| event_type                  | Kiedy                            |
| --------------------------- | -------------------------------- |
| `drive_document_ingested`   | Dokument z Drive zaimportowany   |
| `drive_document_skipped`    | Dokument pominięty               |
| `drive_document_removed`    | Dokument usunięty                |
| `drive_lane_classified`     | Sklasyfikowano lane dokumentu    |
| `drive_case_link_candidate` | Kandydat na powiązanie ze sprawą |
| `drive_conflict_detected`   | Wykryto konflikt                 |
| `drive_extraction_failed`   | Błąd ekstrakcji dokumentu        |

## Actions (`execution_runtime.py`)

| event_type                 | Kiedy                         |
| -------------------------- | ----------------------------- |
| `action_proposal_created`  | Utworzono propozycję akcji    |
| `action_proposal_approved` | Zatwierdzono propozycję akcji |
| `action_proposal_rejected` | Odrzucono propozycję akcji    |
| `action_execution_result`  | Wynik wykonania akcji         |


### Niezmienniki eventów decyzji i wykonania

- `gmail.hitl.send_requested` oznacza żądanie/intent. Nie jest dowodem, że mail został wysłany.
- Trwały execution result jest własnością Node B i jest zapisywany przed completion oraz projekcją.
- Finalny reject używa stabilnej tożsamości eventu powiązanej ze stabilnym `decision_key`; replay tej samej decyzji nie tworzy kolejnego finalnego eventu.
- Replay wykonanej wysyłki lub rejectu może ponowić completion/feed refresh, ale nie może ponowić efektu ani success/reject eventu.
- `failed_before_execution` pozwala na kontrolowany retry. `outcome_unknown` po rozpoczęciu skutku blokuje automatyczny retry.
- Event z `success=true` nie jest sam w sobie dowodem konwergencji UI. Daszek potwierdza wynik dopiero po odczycie świeżej projekcji zgodnej z właściwym `decision_key`.
- Jeżeli emitter jest best-effort, brak eventu nie może unieważniać trwałego execution result; trwały store pozostaje źródłem prawdy.

## Pozostałe

| event_type                     | Emiter                               | Kiedy                                  |
| ------------------------------ | ------------------------------------ | -------------------------------------- |
| `lifecycle.sla_violation`      | `agent_runtime/lifecycle_monitor.py` | Naruszenie SLA cyklu życia             |
| `desk_note_moved_to_case_only` | `desk_maintenance.py`                | Notatka desk przeniesiona do case-only |

## Struktura eventu

`publish_os_event(...)` wstawia wiersz do `unified_os_events` z polami:

```json
{
  "event_id": "osevt_a1b2c3d4e5f6g7h8",
  "event_type": "gmail.feed.pushed",
  "engagement_id": "eng_abc123",
  "source_repo": "gmail-agent",
  "occurred_at": "2026-07-03T12:00:00Z",
  "payload": {
    "schema_version": "topinstal.os_event.v1",
    "summary_pl": "...",
    "status": "ok"
  },
  "correlation": { "case_id": "...", "message_id": "..." },
  "trace_id": "...",
  "span_id": null,
  "parent_event_id": null,
  "case_id": "...",
  "user_id": null,
  "session_id": null,
  "severity": "info",
  "duration_ms": 123,
  "token_usage": null,
  "cost": null,
  "success": true,
  "error_message": null
}
```

`severity` jest normalizowane do `{debug, info, warn, error, critical}` (domyślnie `info`).
`trace_id` jest pobierany z ContextVar (`log_config.get_trace_id`) gdy nie podano jawnie.
`decision_key` nie jest wymaganym polem root każdego OS eventu; dla eventów decyzji musi być jednak obecny w kanonicznej korelacji/payloadzie lub możliwy do jednoznacznego odtworzenia ze stabilnego `event_id`.

## Emitery

- Rdzeń: `event_spine/emitter.py` → `publish_os_event()` (best-effort; zwraca `None` przy błędzie
  lub braku `database_url`, obsługuje `_connection_override` dla transakcyjności).
- Feed / reconcile: `event_spine/gmail_telemetry.py`.
- Pozostałe emitery: patrz kolumna „Emiter" w tabelach powyżej.

## Konsumpcja

Eventy przetwarza `event_spine/processor.py`. Tryb sterowany przez
`event_spine_processor_mode` (domyślnie `off`); gdy `off` a `event_spine_processor_enabled`
jest włączone → efektywnie `shadow`. Dostępne tryby: `off`, `shadow`, `active`.
