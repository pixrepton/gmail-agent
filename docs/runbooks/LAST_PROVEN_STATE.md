# Last Proven State

**Status:** aktywny runbook proof  
**Ostatnia aktualizacja:** 2026-07-17 (`EVAL-RECOVERY-1`)  
**Zakres:** lokalny Docker Compose; bez deployu na VPS/produkcję

## Executive verdict

Lokalny baseline stabilizacji AI-OS TOP-INSTAL ma status **PASS**. Faza 0 (Final Foundation Closeout) zamknięta 2026-07-15 z werdyktem `PASS — FOUNDATION CLOSED` (`knowledge/memory/OPERATOR_DECISIONS.md`); general stabilization jako program jest zamknięty, następny kierunek to Intelligence Evolution — obecnie w toku (`A1 → X1 v0 → EVAL-1 → Roadmap Checkpoint 1 → DELIVERY-1 → EVAL-1.1 rerun → Checkpoint 1.1 → Clean EVAL Rerun → EVAL-RECOVERY-1`).

**`EVAL-RECOVERY-1` (2026-07-17): `PARTIAL — measurement infrastructure complete, capacity pending`.** Naprawiono `generate_draft_reply` argument-schema mismatch (root cause: planner prompt bezwarunkowo instruował dryf w stronę `propose_mutation(operation=generate_draft)`, narzędzia niedostępnego dla mail-agenta — poprawka warunkuje instrukcję realną dostępnością narzędzia; Model A dla `generate_draft_reply` potwierdzony jako właściwy, niezmieniony kontrakt). Zamknięto realne luki pomiarowe w eval harnessie: Understanding output i finalna treść draftu są teraz realnie przechwytywane (potwierdzone żywo pod realnym Groq capacity), production-faithful/component-capability tryby rozdzielone, deterministyczny rubric scoring działa. Pełne artefakty: `C:\ai-os-eval-recovery-1-20260717T205123Z\`, `report.md`.

Udowodniony krytyczny obieg:

```text
Node B Case / decision state
↔ autoryzowany bridge Daszka
↔ wykonanie lub odrzucenie dokładnie raz w granicach kontraktu
↔ trwały wynik
↔ nowa projekcja
↔ finalne potwierdzenie UI
```

Od 2026-07-14/15 do tego chronionego obiegu dołączyła cała rodzina tras mutujących pod jednym kontraktem auth (`agent_runtime.authz.require_mutation_principal`): `/tasks*`, `/engagements/{id}/hitl/approve`, `/engagements/{id}/materialize/approve`, `/agent-chat*`, `GET /cases/{id}/attachments/{ref}`, oraz (2026-07-15, D1) `/identity/binding-suggestions/{id}/status`, `/identity/binding-suggestions/scan`, `/learning/rule-candidates/{id}/status`, `/cases/{id}/operator-action`.

Aktualny werdykt autonomii dla tego obiegu: **YES, WITH EXPLICIT LIMITS**.

## Aktualny baseline

| Obszar | Wynik |
| --- | --- |
| gmail-agent pełny suite (`tools/gmail_audit/tests`) | `1514 passed, 10 skipped, 24 subtests passed, 0 failed` (2026-07-17, `EVAL-RECOVERY-1`; baseline przed sesją 1508/10 — DELIVERY-1 — 0 nowych skipów, +6 nowych testów `test_generate_draft_reply_contract.py`) |
| Daszek pytest | `8 passed` (nie re-zweryfikowane w `EVAL-RECOVERY-1`, bez zmian w Daszku tej sesji) |
| Daszek Node tests (w tym DEC-01) | `13 passed` (jw.) |
| JavaScript / PHP syntax (kalk-top `npm run verify` pełny łańcuch) | PASS (potwierdzone `EVAL-RECOVERY-1`, `scripts\verify-local-gates.ps1` uruchomiony bezpośrednio przez agenta) |
| Workspace gate (`scripts\verify-local-gates.ps1`) | `exit 0` — uruchomiony bezpośrednio przez agenta 2026-07-17 (`EVAL-RECOVERY-1`), zakończone `[OK] verify-local-gates complete` |
| Node B API health | `ok=true` (potwierdzone 2026-07-17 po rebuild/recreate) |
| Worker health | `ok=true`, świeży kontener po rebuild/recreate 2026-07-17 |
| Postgres / Neo4j / Ollama | running, healthy, nietknięte przez rebuild Node B 2026-07-17 |
| API / worker restart count | `0 / 0` (po rebuildzie 2026-07-17: `generate_draft_reply` prompt-contract fix) |
| Host/container SHA-256 | zgodne 2026-07-17 dla zmienionych plików (`agent_runtime/openai_agent_client.py`, `agent_runtime/tool_schemas.py`) |
| FullStack preflight (`-FullStack`) | nie uruchamiany w `EVAL-RECOVERY-1`; ostatni znany wynik `7/7 OK` z 2026-07-13, nie re-zweryfikowany od tego czasu |

Liczby są snapshotem tego proofu, nie obietnicą dla przyszłych zmian. Po zmianie chronionego runtime należy wykonać świeżą regresję i zaktualizować ten dokument.

## Artefakty źródłowe

| Zakres | Status | Artefakty |
| --- | --- | --- |
| Stabilization baseline + worker health | PASS | `C:\gate-b-stabilization-baseline-20260713T004917Z`, `C:\gate-b-worker-health-diagnosis-20260713T015059Z` |
| Row4a / identity / replay | PASS | `C:\gate-b-row4a-s2-final-20260712T210500Z`, `C:\gate-b-row4a-s2-replay-b2-20260712T212706Z` |
| Row4b HITL approve | PASS | `C:\gate-b-row4b-hitl-approve-20260713T000730Z` |
| Case concurrency closeout | PASS | `C:\ai-os-case-concurrency-20260713T084431Z` |
| Naprawa AUTH-01/IDEMP-01/IDEMP-02/DEC-01 | PASS | `C:\ai-os-critical-findings-fix-20260713T122855Z` |
| CONC-01 — atomowa materializacja nowej sprawy | PASS | `C:\ai-os-conc01-new-case-materialization-fix-20260714T144659Z` |
| AUTH-02 / AUTH-03 — fail-open + self-asserted operator_id | PASS | `C:\ai-os-auth02-auth03-fix-20260714T183859Z` |
| AUTH-STREAM-RESIDUAL — `/agent-chat/stream`, `/agent-chat/feedback` | PASS | `C:\ai-os-auth-stream-residual-fix-20260714T201823Z` |
| AUTH-ATTACHMENT-DOWNLOAD-01 — attachment download fail-open | PASS | `C:\ai-os-attachment-download-auth-audit-20260714T221938Z`, `C:\ai-os-auth-attachment-download-fix-20260714T230532Z` |
| Re-audyt sześciu gwarancji (odkrył klaster D1) | READ-ONLY FINDINGS | `C:\ai-os-six-guarantees-reaudit-20260715T005015Z` |
| D1 — cztery trasy Node B bez auth, zamknięte pod `require_mutation_principal` | PASS | `C:\ai-os-d1-nodeb-auth-closeout-20260715T051936Z` |
| Faza 0 Foundation Closeout — D1 runtime proof, naprawa identity-merge (FK violation) i operator-action (connection ownership), DEC-01 durability fix, workspace gate PASS | PASS — FOUNDATION CLOSED | `C:\ai-os-phase0-foundation-closeout-20260715T131348Z` |
| EVAL-1 — pierwszy realny benchmark inteligencji AI-OS | PASS (pomiar) | `C:\ai-os-eval-1-20260716T125141Z` |
| Roadmap Checkpoint 1 — evidence-driven reprioritization | PASS (read-only) | `C:\ai-os-roadmap-checkpoint-1-20260716T205608Z` |
| DELIVERY-1 — RC-1 (provider chain) + RC-2 (tool schema completeness) + failure convergence | PASS | `C:\ai-os-delivery-1-20260717T061112Z` |
| Clean EVAL Rerun — dwa runy, capacity-limited | PARTIAL (uczciwie raportowane) | `C:\ai-os-clean-eval-rerun-20260717T134659Z` |
| EVAL-RECOVERY-1 — `generate_draft_reply` prompt-contract fix (Model A potwierdzony), eval harness measurement-integrity fixes (Understanding/draft capture, mode split, rubric scoring, sentinel suite), 1 realne capacity window (sentinel 6/6 + RUN-A 38 cases, 11/38 unique clean planner) | PARTIAL — measurement infrastructure complete, capacity pending | `C:\ai-os-eval-recovery-1-20260717T205123Z` |

## Zamrożone gwarancje

### Identity i replay

- `signal_id` i `engagement_id` pozostają stabilne przez replay.
- `run_id` oraz techniczne trace ID zmieniają się między wykonaniami.
- `source_signal_ids` zawiera `signal_id`, nie `trace_id`.
- UI nie fabrykuje `trace_id`; brak wiarygodnego trace oznacza `null/missing`.

### Atomowa mutacja Case

Klasyfikacja: `POSTGRES_ATOMIC_MUTATION_CONFIRMED`.

- `PostgresMailboxMemoryStore.mutate_case` używa deterministycznego klucza advisory lock: BLAKE2b 8 B → signed bigint.
- lock, `SELECT ... FOR UPDATE`, mutator, upsert i commit działają na jednym połączeniu i w jednej transakcji.
- dwa niezależne połączenia PostgreSQL zachowały dwie równoległe, niezależne zmiany bez deadlocku.
- wyjątek mutatora powoduje rollback bez częściowego zapisu; lock zwalnia się z końcem transakcji.
- `patch_case_row` używa atomowego kontraktu dla mutacji **istniejącej** sprawy.

**Atomowa materializacja nowego Case** (finding `CONC-01`, naprawiony 2026-07-14, artefakty
`C:\ai-os-conc01-new-case-materialization-fix-20260714T144659Z`):

- `_stamp_case_runtime_state` i `_reconcile_drive_signal`'s `case_seed_row` route obie przechodzą
  przez `mutate_case(case_id, mutator, create_if_missing=True)` — również dla `case_id`, który
  jeszcze nie istnieje w `mailbox_memory_cases`.
- advisory lock serializuje się po `case_id` niezależnie od istnienia wiersza, więc drugi writer
  czeka, a po commit pierwszego odczytuje jego już zapisany rekord pod tym samym lockiem.
- realny dwu-połączeniowy test PostgreSQL potwierdził brak lost update i brak duplikatu wiersza
  dla materializacji nowej sprawy.

### Autoryzacja mutacji Node B

Findingi `AUTH-01`, `AUTH-02`, `AUTH-03`, `AUTH-STREAM-RESIDUAL`, `AUTH-ATTACHMENT-DOWNLOAD-01`, `D1`: **CLOSED / PASS** (wszystkie pod jednym kanonicznym gate `agent_runtime.authz.require_mutation_principal`, zbudowanym na `AUTH-01` `require_mutation_token`).

- aktywne write routes (`/tasks*`, `/engagements/{id}/hitl/approve`, `/engagements/{id}/materialize/approve`, `/agent-chat*`, `GET /cases/{id}/attachments/{ref}`, oraz od 2026-07-15 `/identity/binding-suggestions/{id}/status`, `/identity/binding-suggestions/scan`, `/learning/rule-candidates/{id}/status`, `/cases/{id}/operator-action`) są fail-closed;
- brak, błędny lub read-only token kończy się `401` przed walidacją biznesową i przed store;
- brak konfiguracji auth nie otwiera mutacji;
- tożsamość operatora nie jest przyjmowana bezwarunkowo z dowolnego pola requestu — spoofed `operator_id`/`approved_by`/`reviewed_by` w body jest zawsze ignorowany na rzecz zweryfikowanego principala (potwierdzone realnym live API proof 2026-07-15 dla D1: `learning_rule_candidates.approved_by`, `identity_merge_log.operator_id`).
- `D1` (2026-07-15): klaster czterech tras odkryty w re-audycie sześciu gwarancji jako całkowicie pozbawiony auth dependency, zamknięty pod tym samym gate. Real live API proof: negatywne ścieżki 401 z zerową mutacją; poprawny credential wykonuje realną mutację (rule candidate approve, identity merge, `operator_response_records` insert).

### Identity merge i operator-response — atomowość i connection ownership (naprawione 2026-07-15)

Podczas real live-API proof D1 wykryto, że dwie z czterech tras (`/identity/binding-suggestions/{id}/status`, `/cases/{id}/operator-action`) wykonywały trwałą, zacommitowaną mutację i **mimo to zwracały HTTP 500**. Oba naprawione tego samego dnia; artefakty: `C:\ai-os-phase0-foundation-closeout-20260715T131348Z\bug-fix-analysis.json`.

- **Identity merge** (`execute_identity_merge`): `identity_merge_log` był zapisywany PO usunięciu source identity, które kaskadowo (`ON DELETE CASCADE`) kasowało wiersz `identity_binding_suggestions`, do którego log odwoływał się przez FK (`ON DELETE SET NULL`) — zawsze `ForeignKeyViolation`. Naprawa: `store.merge_identities()` wykonuje repoint + zapis logu + delete w jednej transakcji, log zapisywany PRZED delete.
- **Operator-response chain** (`record_operator_response`, `record_agent_proposal`, `maybe_create_learning_candidate`, `store_pattern_candidates`): używały `with conn:` na połączeniu nie będącym ich własnością — w zainstalowanej wersji psycopg to zamyka połączenie na wyjściu z bloku, łamiąc dalsze operacje callera. Naprawa: goły `with conn.cursor()` + jawny `conn.commit()`.
- Real-Postgres regression testy (`test_identity_merge_atomic_postgres.py`, `test_operator_action_connection_ownership_postgres.py`) potwierdzone RED (dokładnie te same wyjątki) → GREEN.
- Retry bezpieczny na obu trasach: 404 dla identity merge (kaskada strukturalnie wyklucza drugi merge); pusty `results` dla operator-action (brak otwartej propozycji po pierwszej odpowiedzi).

### Idempotencja decyzji i skutków

Findingi `IDEMP-01` i `IDEMP-02`: **CLOSED / PASS**.

- send i reject używają stabilnego `decision_key`/execution identity;
- wynik wykonania jest trwały i oddzielony od completion/feed projection;
- sukces executora + failure completion + replay nie uruchamia executora drugi raz;
- dwa równoległe drainery nie wykonują send/reject dwukrotnie;
- `failed_before_execution` może zostać ponowiony;
- `outcome_unknown` po rozpoczęciu skutku blokuje automatyczny retry;
- reject tworzy jeden stabilny finalny event i jawnie obsługuje konflikt finalnych decyzji.

### Konwergencja UI

Finding `DEC-01`: **CLOSED / PASS**. Test JS (`daszek/tests/test_row4b_note_hitl_approve.node.js`, 13/13) jest `DURABLE_AFTER_CURRENT_PATCH` (patrz `knowledge/memory/OPERATOR_DECISIONS.md` 2026-07-15) — nadal untracked w git, ale odtwarzalny przez realny, git-apply-owalny patch, nie ręczną kopię.

- HTTP 200/`accepted` nie jest finalnym potwierdzeniem skutku;
- UI pokazuje stan przyjęcia/oczekiwania, dopóki aktualna projekcja nie potwierdzi tego samego `decision_key` i statusu końcowego;
- stary feed, timeout lub feed push failure nie daje finalnego sukcesu;
- ponowne kliknięcie jest blokowane w czasie oczekiwania na konwergencję;
- refresh odtwarza stan z Node B, nie z lokalnego toastu.

## Ograniczenia jawne

- Proof dotyczy środowiska lokalnego. Nie stanowi dowodu wdrożenia VPS/produkcyjnego.
- Gmail nie zapewnia aplikacyjnemu runtime uniwersalnego idempotency key dla skutku zewnętrznego. Gwarancją jest bezpieczny kontrakt: retry przed skutkiem, no-retry po skutku potwierdzonym lub nieznanym.
- `outcome_unknown` wymaga jawnego recovery/operator review.
- `DEC-01` i większość testów freeze-guarantee w gmail-agent pozostają niescommitowane w git (`git_push_pending`) — trwałość dowiedziona przez clean-worktree/patch simulation, nie przez faktyczny commit.
- Zwiększanie autonomii jest dozwolone wyłącznie w granicach chronionego auth, policy, stabilnego decision key, trwałego execution result i konwergencji UI.

## Reguła następnych zmian

Chronionego obiegu nie zmieniaj „przy okazji”. Każda zmiana wymaga:

1. reprodukcji albo jasno zdefiniowanej nowej gwarancji;
2. testu RED;
3. minimalnej poprawki;
4. testu GREEN i pełnej regresji;
5. runtime parity/health, jeśli kod jest bake'owany w obrazie;
6. aktualizacji LPS dopiero po PASS.
