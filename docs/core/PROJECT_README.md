# Gmail Agent / TOP-INSTAL AI-OS — README produktu i developera

> **Node A canonical:** `../daszek/` + `../wp-bridges/`. **Node B:** repo `gmail-agent`, Postgres mailbox memory i signal runtime.

**Status:** aktywny przewodnik po aplikacji. **Wersja:** 2026-08-20.
**Stan żywy:** `../../../knowledge/memory/ACTIVE_WORKSPACE.md`  
**Proof runtime:** [`../runbooks/LAST_PROVEN_STATE.md`](../runbooks/LAST_PROVEN_STATE.md)

**Zakres:** aktualny model Node B, granice warstw, punkty startowe w kodzie i reguły bezpiecznego rozwoju.  
**Nie jest:** runbookiem deployu, dowodem produkcyjnym ani zgodą na autonomiczne skutki zewnętrzne.

Przy rozbieżności: Konstytucja V2.1 → aktualny kod i test → LPS dla runtime → ten README.

## 0. Stan na dziś (2026-07-13)

| Obszar | Status | Dowód |
| --- | --- | --- |
| Stabilization baseline | **PASS** | `docs/runbooks/LAST_PROVEN_STATE.md` |
| FullStack preflight | **7/7 OK** | lokalny proof 2026-07-13 |
| Case concurrency | **POSTGRES_ATOMIC_MUTATION_CONFIRMED** | `C:\ai-os-case-concurrency-20260713T084431Z` |
| `/tasks*` mutation auth | **fail-closed / PASS** | AUTH-01 closeout |
| HITL send/reject replay safety | **PASS** | IDEMP-01 / IDEMP-02 closeout |
| UI execution confirmation | **convergence-gated / PASS** | DEC-01 closeout |
| gmail-agent full suite | patrz LPS (zmienia się między sesjami) | LPS snapshot |
| Workspace gate | **exit 0** | wykonany po rebuild/recreate |
| Runtime | API/worker/Postgres healthy | host/container parity zgodne |

**Deployment:** lokalny Docker only. VPS/produkcja pozostają zawieszone. Liczby testów są snapshotem proofu; po zmianie runtime źródłem aktualnego wyniku jest LPS.

### 0a. Gwarancje zamknięte stabilizacją

- atomowa mutacja istniejącego Case przez `PostgresMailboxMemoryStore.mutate_case`;
- stabilny advisory lock, jedna transakcja i jeden connection boundary;
- fail-closed auth dla aktywnych write routes `/tasks*`;
- stabilny `decision_key` i osobne stany `accepted`, execution, completion i convergence;
- trwały execution result przed completion/projection;
- send i reject bez podwójnego skutku przy replayu lub równoległym drainie;
- retry skutku tylko po `failed_before_execution`;
- brak automatycznego retry po `outcome_unknown`;
- finalny sukces w Daszku dopiero po świeżej projekcji z matching `decision_key`.

## 1. Po co istnieje ta aplikacja

`gmail-agent` to backendowy system operacyjny komunikacji TOP-INSTAL (**Node B**). Jego zadaniem nie jest tylko „ładnie odpisać na maila”. System zamienia napływające sygnały z Gmaila, Drive, Calendar, Daszka i kanałów cross-repo (`kalk-top`/Cieplo) w **sprawy (cases)**, **fakty w pamięci**, **kontekst pod decyzję**, **ujawnialne dowody (EvidenceRef)**, **zrozumienie sytuacji**, **kandydata decyzji**, **politykę**, **propozycję akcji po policy**, **projekcję operatorską (feed v3 / Decision View / Skrzat)** oraz **feedback operatora** — w sposób możliwy do audytu i replayu.

### Oś danych (co oznacza każdy krok)

Poniższa oś jest **modelem logicznym** zgodnym z kierunkiem przepływu w kodzie; konkretne nazwy pól i zapis w journalu/store różnią się per komenda, ale kolejność „od surowego sygnału do projekcji” jest stała.

**Profil runtime steruje głębią.** `CASE_OS_RUNTIME_PROFILE` (`config.py`) domyślnie = **`full`**: włącza flagi Case OS, `AGENT_RUNTIME_ENABLED=1`, `DECISION_PIPELINE_DRY_RUN_ONLY=0`, `DASZEK_FEED_SOURCE=engagement_snapshot_v2`. Profil `minimal` (albo `EMERGENCY_INTELLIGENCE_KILLSWITCH=1`) zeruje te flagi. Łańcuch `UnderstandingOutput → DecisionCandidate → PolicyDecision → ActionProposal` **istnieje w kodzie i jest gated** flagami (`UNDERSTANDING_OUTPUT_ENABLED`, `DECISION_PIPELINE_ENABLED`, `ACTION_PROPOSAL_V2_ENABLED`).

```text
mail / dokument / zdarzenie
-> RawObservation / CanonicalSignal        (raw_observation_contract, signal_contract, signal_journal)
-> reconcile signal (active spine)          (signal_reconciler -> run_shared_downstream_stages)
-> Case Memory / Hot State                  (mailbox_memory/ pakiet, case_snapshot_manager)
-> Evidence / Context Build                 (case_context_contract, evidence_ref, context_quality_contract)
-> Case Intelligence (desk/risks/NBA/...)   (case_intelligence/ pakiet)
-> (gated) UnderstandingOutput -> DecisionCandidate -> PolicyDecision -> ActionProposal v2
-> Correlation Registry / Engagement        (correlation_registry/, case_engagement_bridge)
-> Operational Feed v3 (schema 1.3)         (daszek_v3_operational_feed -> daszek_client -> Node A)
-> Daszek Operator Review
-> Feedback / Replay / Metrics              (operator_feedback_runtime, signal_reconciler, case_state_rebuilder)
```

| Krok na osi       | Intuicja operatorska                                             | Gdzie to typowo żyje w kodzie (punkt startowy)                                                                       |
| ----------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| surowe wejście    | „Co przyszło z kanału?”                                          | `raw_observation_contract.py`, adaptery (`gmail_signal_adapter.py`, `calendar_signal_adapter.py`, …)                 |
| sygnał kanoniczny | „Jedna jednostka pracy dla Node B”                               | `signal_contract.py`, `signal_journal.py`                                                                            |
| reconcile         | „Odtworzenie stanu sprawy z sygnału”                             | `signal_reconciler.py` (`reconcile_signal` → rejestr handlerów), `intake_shared_downstream.py`                       |
| pamięć sprawy     | „Co wiemy o kliencie/sprawie w czasie”                           | pakiet `mailbox_memory/` (`postgres.py`, `protocol.py`, `schema.py`), `mailbox_memory_runtime.py`                    |
| kontekst + dowody | „Co wolno pokazać operatorowi jako uzasadnienie”                 | `case_context_contract.py`, `evidence_ref.py`, `context_quality_contract.py`                                         |
| inteligencja      | „Desk card, ryzyka, braki, next-best-action”                     | pakiet `case_intelligence/` (`orchestrator.build_case_intelligence`, `desk.build_desk_composition`)                  |
| zrozumienie       | „Co LLM/ekstrakcja uważa za sytuację — bez wykonania”            | `understanding_output.py`, `business_reasoner.py`, `case_guidance_reasoner.py`                                       |
| kandydat decyzji  | „Formalna propozycja decyzyjna przed policy” (gated)             | `decision_candidate.py`, `decision_pipeline.py`                                                                      |
| polityka          | „Czy wolno w ogóle typ akcji”                                    | `policy_engine.py`, `policy_decision.py`, `policy_action_proposal.attach_policy_and_proposals`                       |
| propozycja akcji  | „Co konkretnie robimy, jeśli policy pozwala” (gated)             | `action_proposal_v2.py`                                                                                              |
| korelacja         | „Jeden klient/engagement spina sprawy z różnych repo”            | `correlation_registry/` (`service.py`, `store.py`), `case_engagement_bridge.py`                                      |
| Daszek            | „Bezpieczna projekcja + review”                                  | `daszek_v3_operational_feed.py`, `dash_projection_v2.py`, `decision_projection_blocks.py`, `../daszek/public/app.js` |
| feedback / replay | „Operator koryguje kalibrację albo prawdę; system odtwarza stan” | `operator_feedback_runtime.py`, `feedback_event_contract.py`, `signal_reconciler.py`, `case_state_rebuilder.py`      |

System ma odciążać właścicieli i operatorów TOP-INSTAL przez:

- porządkowanie maili i spraw (powiązanie wątków, `case_id`, snapshoty, engagement),
- wykrywanie typu sprawy, priorytetu i braków (klasyfikatory + scorer — deterministyczne reguły tam, gdzie są),
- zbieranie kontekstu z pamięci i dokumentów (CaseContextPack, Drive ingest),
- pokazywanie operatorowi **projekcji** w Daszku (skróty, etykiety, brak raw body w dowodach),
- pilnowanie, żeby **LLM nie stał się źródłem prawdy** (EvidenceRef, walidacja JSON, policy),
- wymuszanie **policy gate** przed ryzykowną akcją,
- zapisywanie feedbacku operatora do dalszej kalibracji (osobno od adjudykacji prawdy).

## 2. Krótki opis dla użytkownika/operatora

Operator nie pracuje bezpośrednio w Pythonie. Dla operatora aplikacja objawia się głównie przez **Daszek** (Node A) i proces obsługi spraw; Node B dostarcza dane przez bridge / feed zgodnie z konfiguracją.

### Model Case / Task widziany przez operatora

Operator nie widzi „tasków” jako osobnego bytu — widzi **sprawy** i **elementy wymagające akcji** (`action_items`) w ramach spraw:

| Pojęcie operatorskie    | W kodzie / feedzie                                                                                      |
| ----------------------- | ------------------------------------------------------------------------------------------------------- |
| Sprawa (case)           | wiersz `mailbox_memory_cases`; `case_family` (np. `lead_opportunity`, `sales`, `operations`, `service`) |
| Wymaga akcji            | `metadata.requires_action` = `True` (obliczane przez `case_routing.case_row_requires_action`)           |
| Element akcji           | `feed.action_items` (schema 1.3) — **nie** osobna tabela „tasks”                                        |
| „Zadanie” z UI (manual) | POST `/tasks` tworzy sprawę `case_family=operations`, `source_kind=manual` przez `case_write_gateway`   |
| Na Biurku (desk)        | `metadata.desk_eligible` (pasmo P1/P2, `case_routing.desk_eligible`) → `feed.desk`                      |

Endpoint `GET /tasks` jest **deprecated shim** — zwraca `"deprecated": true` i podpowiada migrację na `GET /cases?requires_action=true&source_kind=manual`.

### Co system potrafi (z zastrzeżeniami)

| Zdolność z punktu widzenia operatora              | Uwaga prawdy (nie obniża wartości, ale ogranicza oczekiwania)                                                                   |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| odebrać i przeanalizować wiadomość z Gmaila       | Wymaga skonfigurowanego dostępu Google; tryby `doctor`/`message`/`period` mają różne koszty i mutacje pamięci                   |
| podpiąć wiadomość do sprawy lub utworzyć nową     | Zależy od `case_linker` / tożsamości / correlation registry; nie gwarantuje braku false-positive bez review                     |
| pokazać sedno, priorytet, ryzyka, braki           | „Sedno” w Decision View ma **precedencję pól** zdefiniowaną w kodzie (`decision_projection_blocks.py`), nie dowolny LLM-summary |
| odróżnić lead / serwis / admin / dostawcę / szum  | To są **etykiety modelu/reguł** (`mail_classification.classify_message`) — operator nadal zatwierdza sens biznesowy             |
| wskazać braki do decyzji lub odpowiedzi           | `ContextQuality` i gaps w packu — opisują gotowość, nie „jakość firmy”                                                          |
| zebrać fakty z mailbox memory                     | Postgres jest operacyjną prawdą; retrieval (pgvector) jest pomocniczy                                                           |
| dołączyć kontekst z Drive                         | Osobny bounded tor ingestu; nie zastępuje decyzji ani policy                                                                    |
| pokazać decyzję/propozycję bez udawania wykonania | UI i copy są „draft / propozycja”; execution jest osobnym gate (HITL / materialize)                                             |
| przygotować szkic odpowiedzi                      | Tylko gdy lane i policy na to pozwalają; domyślnie bezpieczna degradacja                                                        |
| zadać pytanie o sprawę (Skrzat)                   | Read-only: `POST /cases/{id}/skrzat/ask` (Node B) lub proxy WP; wymaga `DASZEK_NODE_B_API_BASE` na Node A                       |
| czatować z agentem o sprawie                      | `POST /agent-chat` / `/agent-chat/stream` (SSE) — auth bearer + rate-limit 30/min; agent proponuje, nie wykonuje bez HITL       |
| przyjąć feedback z Daszka                         | `operator-feedback` vs `daszek-bridge-drain` — różne ścieżki wejścia; patrz help w CLI                                          |
| odtworzyć / zreconcile'ować stan                  | Wymaga journalu + konfiguracji; `dry_run` celowo nie mutuje części świata                                                       |

Operator powinien traktować Daszek jako **pulpit operacyjny**, nie jako źródło prawdy. Daszek pokazuje projekcję przygotowaną przez Node B; „prawda operacyjna” siedzi w Postgres / journalu / regułach merge, nie w tekście na ekranie.

## 2a. Model mentalny developera (zanim wejdziesz w `gmail_intake.py`)

### Rozbicie god-classów na pakiety (Enterprise Quality Sprint, 07.2026)

Trzy monolity zostały rozbite na pakiety z fasadą + shim zgodności:

| Było (monolit)                      | Jest (pakiet)                                                                                                                                       | Shim zgodności                                                          |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `case_intelligence.py` (~1753 L)    | `case_intelligence/` (`orchestrator`, `desk`, `risks`, `missing_info`, `next_best_action`, `understanding`, `lifecycle`, `validators`, `constants`) | `_case_intelligence_legacy.py`                                          |
| `mailbox_memory_store.py` (~2546 L) | `mailbox_memory/` (`protocol`, `schema`, `postgres`, `inmemory`, `facts`)                                                                           | `mailbox_memory_store.py` (32 L re-export), `_mailbox_memory_legacy.py` |
| `gmail_intake.py` argparse          | `gmail_intake_parser.py` (`build_parser`), `gmail_intake_process.py` (`process_snapshot`), `gmail_intake_doctor.py`                                 | importy „late” w `gmail_intake.py`, żeby zerwać cykle                   |

`gmail_intake.py` to nadal duży (~4800 L) router CLI + orchestracja, ale ciężkie ciała komend są wyniesione.

### Lane i plan etapów

Preklasyfikacja (`preclassifier.preclassify_snapshot`, wołana z `gmail_intake.py`) ustawia `lane`. Na tej podstawie `_build_lane_stage_plan` (`gmail_intake.py`) zwraca **plan etapów**:

| `lane`                                                 | `intake_reasoning_mode`           | `run_case_linking` | `run_business_reasoning` | `run_reply_drafter` | `run_action_planner` | `expected_projection_mode` |
| ------------------------------------------------------ | --------------------------------- | ------------------ | ------------------------ | ------------------- | -------------------- | -------------------------- |
| `skip`                                                 | `deterministic_preclassification` | nie                | nie                      | nie                 | tak                  | `ignore`                   |
| `reference_only`                                       | `deterministic_preclassification` | tak                | nie                      | nie                 | tak                  | `reference`                |
| `review_direct`                                        | `deterministic_preclassification` | tak                | nie                      | nie                 | tak                  | `review`                   |
| `intake_llm` (domyślny / fallback jeśli lane nieznany) | `llm`                             | tak                | tak                      | tak                 | tak                  | `""` (brak wymuszenia)     |

### Signal runtime (`SIGNAL_RUNTIME_MODE`) — **tylko `active`**

**Zmiana vs 2026-05:** `process_snapshot` (`gmail_intake_process.py`) honoruje **wyłącznie** `active`. Każda inna wartość (`legacy`, `shadow`) → **`ConfigError`** („Gmail processing requires signal-active spine … Legacy process_snapshot tail was removed (Epik 5 / CEL)”). Nie ma już inline'owego legacy ogona.

| Wartość  | Zachowanie                                                                    |
| -------- | ----------------------------------------------------------------------------- |
| `active` | `run_gmail_signal_runtime` → `reconcile_signal`; **jedyna** wspierana ścieżka |
| `legacy` | **`ConfigError`** — usunięte                                                  |
| `shadow` | **`ConfigError`** — usunięte                                                  |

### Dispatch reconcile przez rejestr (nie if/elif)

`signal_reconciler.reconcile_signal` deleguje do **rejestru** `agent_runtime.signal_registry.SIGNAL_HANDLERS`. Handlery rejestrowane dekoratorem:

- `@register_signal_handler("gmail")` → `_reconcile_gmail_signal` → **`run_shared_downstream_stages`**,
- `@register_signal_handler("drive")` → `_reconcile_drive_signal` → też `run_shared_downstream_stages` (z `skip_draft_reply`).

**`run_shared_downstream_stages`** (`intake_shared_downstream.py`) — kolejność etapów:

1. `link_case_context`
2. `ingest_mailbox_memory`
3. `run_business_reasoning` (LLM)
4. `draft_reply` (LLM; pomijane dla Drive)
5. `plan_actions`
6. `build_case_intelligence_layer` (LLM, gated flagami)
7. `finalize_mailbox_memory`
8. hot-state: `legacy_inject` **lub** `reconcile_signal_apply` (`CaseSnapshotManager.apply_signal` + `apply_hot_state_to_case_intelligence`)
9. **`attach_policy_and_proposals`** (jedyne policy attach w tej ścieżce)

Zwraca `SharedDownstreamResult` (`policy_report`, `policy_action_proposal`, `stage_timings_ms`).

### BusinessReasoning: deterministic clarification signal (2026-08-20)

`run_business_reasoning` przekazuje `intake_result` do
`parse_and_validate_business_reasoning`, a
`intake_schema.validate_business_reasoning_result` wylicza deterministyczny
sygnał `customer_clarification_possible`. Gdy intake już oznaczył sygnał
serwisowy jako `ambiguous_signal`, review wynika z braków danych klienta, a BR
nazwał konkretne `missing_information`, walidator normalizuje
`escalate_review -> collect_data` (tylko dla stanu `unclear`/`waiting_for_data`).

Reguła nie używa `hvac_intent`, case-id ani globalnego promptu. Jest
jednokierunkowa i nie zmienia intake `review_required`; dzięki temu drafter nie
jest pomijany dla niejednoznacznych zgłoszeń serwisowych, a prawdziwe
eskalacje (`SVC-01`, `SVC-02`, `MI-03`, `DEC-01`) pozostają bez zmian.
Test: `tools/gmail_audit/tests/test_svc05_customer_clarification.py`.

### Artefakty etapowe (jedna wiadomość / sygnał)

`_build_stage_record` / `_append_stage_artifacts` grupują wyniki pod kluczami m.in.: `preclassification_results`, `intake_results_raw`, `intake_results_final`, `case_link_results`, `business_reasoning_results`, `reply_draft_results`, `action_plan_results`, `case_intelligence_results`, `mailbox_memory_results`, `case_guidance_results`, `attachment_intelligence_results`, `thread_memory_results`, `projection_previews`, `signal_projections`, `case_patches`, `desk_note_patches`, `decision_traces`, `review_decisions`, `execution_metadata`. To **mapa miejsc**, gdzie szukać regresji: zmiana kontraktu na jednym etapie często wymaga testu na kolejnym albo w proof-packu.

## 2b. Model Case / Task / Engagement (kanoniczny, AR-2026)

To jest sedno zmian z lipca 2026 i najczęstsze źródło pomyłek. Trzy warstwy tożsamości:

| Warstwa        | Tabela                  | Rola                                                                                                        |
| -------------- | ----------------------- | ----------------------------------------------------------------------------------------------------------- |
| **Identity**   | `topinstal_identities`  | Osoba/klient; `primary_email` **bez** UNIQUE (jedna osoba = wiele inwestycji)                               |
| **Engagement** | `topinstal_engagements` | Time-bounded **hub korelacji** dla `identity_id`; kotwica cross-repo (`status` default `open`, `anchor_at`) |
| **Case**       | `mailbox_memory_cases`  | Operacyjna **sprawa** Node B (`case_id`, routing, fakty, metadata)                                          |

**`correlation_links`** to krawędzie: `(link_type, target_id, source_repo) → engagement_id` z `confidence`. Bridge zewnętrznych ID (gmail_message, mailbox_case, cieplo_workflow, …) do **jednego** engagement.

### Reguły zapisu (twarde)

- **Kanoniczny pełny writer:** `case_write_gateway.write_case_row(...)` — waliduje routing i wykonuje kontrolowany full write.
- **Kanoniczna mutacja istniejącego Case:** `case_write_gateway.patch_case_row(...)` → `PostgresMailboxMemoryStore.mutate_case(...)`.
- `mutate_case` obejmuje advisory lock, `SELECT ... FOR UPDATE`, mutator, upsert i commit na jednym połączeniu/transakcji.
- `_stamp_case_runtime_state` w reconcilerze również korzysta z atomowej mutacji.
- Stale `fetch → merge → full upsert` poza lockiem jest zabronione dla równoległych writerów.
- `operator_priority_to_label`: `pilne` → `P1 - pilne`, `niski` → `P3 - niski`, pozostałe → `P2 - ważne`.
- `RegistryLinkConflictError` blokuje ciche przepięcie istniejącego `mailbox_case` do innego engagementu.
- Merge/link pozostaje keyed by `engagement_id`, nie przez przypadkowe podobieństwo `case_id`.

Proof atomowości: `docs/runbooks/MAILBOX_MEMORY_POSTGRES.md` i `C:\ai-os-case-concurrency-20260713T084431Z`.

### Materialize / HITL (write po zatwierdzeniu)

`agent_runtime/materialize.py` — kanoniczny writer po HITL approve (RFC E2):

- `append_materialize_proposal(...)`, `execute_materialize_proposal(...)`,
- `ptype=="create_case"` → `classify_mailbox_row` + `_register_engagement_link` (rejestruje `mailbox_case` w registry),
- `_execute_composite_step` — plany wieloetapowe.
- `agent_runtime/tools/write_executors.py`: `execute_create_case`, `execute_update_case_status`, `execute_merge_cases`, drafts, lifecycle.

### `case_engagement_bridge.py`

- `resolve_engagement_id(case_id, registry_store=)` → po `mailbox_case` link (`source_repo="gmail-agent"`),
- `resolve_case_id(engagement_id, registry_store=)` → pierwszy `mailbox_case` link engagementu.

## 2c. Bezpieczny cykl decyzji operatora

Kanoniczny lifecycle:

```text
received → accepted → executing → executed | outcome_unknown | failed_before_execution → converged
received → accepted → rejected → converged
```

Zasady:

- `accepted` oznacza przyjęcie komendy, nie wykonanie skutku;
- jedna decyzja ma stabilny `decision_key`/queue identity;
- execution result jest trwały i zapisany przed completion/feed refresh;
- replay `executed`/`rejected` nie uruchamia executora ani drugiego finalnego eventu;
- `failed_before_execution` może być bezpiecznie ponowiony;
- `outcome_unknown` po rozpoczęciu skutku blokuje automatyczny retry;
- dwa równoległe drainery nie wykonują tej samej decyzji dwa razy;
- Daszek pokazuje finalny sukces dopiero po konwergencji świeżej projekcji.

Właściciele: `agent_hitl_bridge.py`, `hitl_gmail_send.py`, `execution_runtime.py`, `daszek_bridge_queue_drain.py`, `agent_runtime/authz.py`, `api_app.py` oraz po stronie Node A `api-v3-handlers.php` i `public/app.js`.

## 3. Co jest już w systemie

Poniżej opisuje stan repo i lokalnie zaimplementowane możliwości. Obecność kodu **nie** oznacza automatycznie, że dana funkcja działa na produkcji ani że jest potwierdzona (patrz sekcja 6, `LAST_PROVEN_STATE.md`).

**Stan proof 2026-07-13:** pełny pakiet `tools/gmail_audit/tests` jest zielony zgodnie z LPS. Nie kopiuj liczby do nowych dokumentów ani commit messages jako trwałej cechy; po każdej zmianie runtime uruchom świeży suite i workspace gate.

### Runtime spine (active-only)

| Element                        | Moduł / funkcja                                                        | Rola                                                                                       |
| ------------------------------ | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Reconcile dispatch             | `signal_reconciler.reconcile_signal` + `agent_runtime.signal_registry` | Rejestr handlerów `gmail`/`drive` (dekorator), zamiast if/elif                             |
| Wspólny downstream             | `intake_shared_downstream.run_shared_downstream_stages`                | Jedna kolejność link → memory → business → reply → plan → intelligence → finalize → policy |
| Pojedyncze policy attach       | `policy_action_proposal.attach_policy_and_proposals`                   | Wołane z shared downstream; `build_case_intelligence_layer` **nie** woła `evaluate_policy` |
| Operator projection transport  | `projection_snapshot_transport.build_operator_projection_snapshot`     | v2 + `decision_view` + trays; używane przez `build_v2_projection` i Gmail reconcile        |
| Case intelligence orchestrator | `case_intelligence.orchestrator.build_case_intelligence`               | DAG: lifecycle → missing → risks → NBA → understanding → brief → desk → validate           |
| Kanoniczny writer sprawy       | `case_write_gateway.write_case_row` / `patch_case_row`                 | Jedyna droga do `upsert_case` w runtime API                                                |
| Correlation registry           | `correlation_registry.service.CorrelationRegistryService`              | Identity/engagement/links, snapshot engagementu                                            |
| Feed operacyjny v3             | `daszek_v3_operational_feed` (schema **1.3**)                          | `desk`/`cases`/`action_items`/`case_details`/`day`; push przez `daszek_client`             |

### Context Projection + Skrzat (read-only)

| Moduł                                               | Rola                                                                                                   |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `context_tray_set.py`                               | `build_context_tray_set` — tacki kontekstu (`context_tray_set.v1`) z packa/CI                          |
| `projection_envelope.py`, `projection_validator.py` | Kontrakt envelope + walidacja projection-safe                                                          |
| `daszek_projection_router.py`                       | Mapowanie envelope → widoki Daszek (Biurko/Sprawa/…)                                                   |
| `skrzat_runtime.py`                                 | `answer_case_question` — deterministyczny fallback envelope z `ContextTraySet`                         |
| `skrzat_copilot.py`                                 | `resolve_skrzat_answer` — `context_audit.assembled_context`, opcjonalny LLM (`SKRZAT_ANSWER_MODE=llm`) |
| `email_personalizer.py`                             | `run_email_personalization` — `POST /internal/email/personalize-offer`                                 |
| `projection_quality_metrics.py`                     | Metryki jakości projekcji / Skrzat                                                                     |
| `api_app.py`                                        | FastAPI Node B (pełna lista endpointów w §3f)                                                          |
| `../daszek/public/app.js`                           | Panel Skrzat + agent-chat (UI); woła Node B przez `DASZEK_NODE_B_API_BASE` lub WP proxy                |

### Gmail intake (`gmail_intake.py`) — podkomendy CLI

Jedyny duży **CLI entrypoint** Node B. Parser w `gmail_intake_parser.build_parser` (parent parser `common` z flagami wspólnymi). Dispatch: `if args.command == …` w `main()`, fallback `_run_pipeline`.

| Grupa                  | Komenda                                                               | Krótki opis                                                                         |
| ---------------------- | --------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Analiza Gmail (legacy) | `message` / `batch` / `period` / `shadow-run`                         | **ConfigError** przy signal-active (domyślne) — użyj `signal-run` / `signal-worker` |
| Replay / cohort        | `rerun`                                                               | Rerun na frozen snapshotach (read-oriented)                                         |
| Diagnostyka            | `doctor`                                                              | Config + opcjonalnie Gmail/Drive/Daszek/Calendar (`--check-*`, `--skip-gmail`)      |
| Jakość / replay        | `eval`, `eval-summary`, `replay-v2`, `push-memory-v2`                 | Ocena adnotacji / agregacja metryk / replay v2 / push memory-v2                     |
| Daszek desk            | `maintain-desk`                                                       | `--preview` lub `--apply` deterministycznego maintenance                            |
| Pamięć                 | `memory-backfill`, `gmail-bootstrap-history`                          | Backfill / ograniczony bootstrap historii (flagi bezpieczeństwa)                    |
| Kontekst sprawy        | `case-context`                                                        | Read-only pack dla `--case-id`/`--message-id`; opcjonalny pilot Neo4j               |
| Akcje nadzorowane      | `action-proposal-list` / `-approve` / `-reject` / `-execute`          | CRUD + execute przez policy gate (`--dry-run` na execute)                           |
| Kalendarz              | `calendar-ingest`, `calendar-context`                                 | Odczyt/ingest vs blok kontekstu                                                     |
| Dokumenty              | `document-intelligence`                                               | Parser-text DI V1 na fixture                                                        |
| Drive                  | `drive-ingest`, `drive-case-context`, `drive-graph-rebuild`           | Ingest read-only / pack z Drive / przebudowa grafu                                  |
| Dowody kohortowe       | `cohort-proof`                                                        | Bounded proof record (uwaga na `--ingest-selected` — mutuje pamięć)                 |
| Sygnały                | `signal-run`, `signal-worker`, `signal-replay`, `signal-rebuild-case` | Oneshot vs pętla (`run_signal_loop`), replay po `signal_id`, rebuild sprawy         |
| Event spine            | `event-spine-processor`                                               | Przetwarzanie event spine                                                           |
| Agent MCP              | `agent-mcp-serve`                                                     | Serwer MCP dla agenta (debug)                                                       |
| Feedback / bridge      | `operator-feedback`, `daszek-bridge-drain`                            | JSON/stdin → zdarzenia pamięci; drain kolejki bridge                                |
| Zmiany źródeł          | `gmail-detect-changes`, `drive-detect-changes`                        | History/Changes API + persystencja kursora                                          |
| Słownik biznesowy      | `bizdict-extract` / `-search` / `-sync` / `-outbox-process`           | Delegacja do `business_dictionary.cli`                                              |
| SLA / cleanup          | `sla-watcher`, `os-events-cleanup`                                    | Watcher SLA (oneshot/loop) / TTL cleanup zdarzeń OS                                 |

**Token wymagania** (`_requires_google_token`, `run_live_command`): mutujące ingress Gmail (`message`/`period`/`shadow-run`/`batch`) są **zablokowane** (`gmail_ingress_guard`); live Gmail idzie przez `signal-run` / `signal-worker`. Groq/LLM (`require_groq=True`) dla pipeline'u, `rerun`, `signal-run/-worker`. Większość komend read/admin: `require_groq=False, require_google=False`. Przed live Gmail: `doctor`.

### Triage i klasyfikacja

| Moduł                     | Rola techniczna                                        | Kluczowe API                                                                                                             |
| ------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------ |
| `preclassifier.py`        | Szybka ścieżka deterministyczna: lane + plan etapów    | `preclassify_snapshot`, `is_obvious_noise`, `is_reference_only`                                                          |
| `mail_classification.py`  | Scoring export/bootstrap                               | `classify_message` → candidate/case_type/priority_label                                                                  |
| `topic_classifier.py`     | Temat / routing tekstowy dla decision pipeline         | `build_topic_result`                                                                                                     |
| `case_type_classifier.py` | Typ sprawy + hinty adjudykacji linku                   | `build_case_type_result`                                                                                                 |
| `priority_sla_scorer.py`  | Priorytet i SLA jako struktura, nie „kolor w UI”       | `build_priority_sla_result`                                                                                              |
| `decision_pipeline.py`    | Skleja UnderstandingOutput → DecisionCandidate (gated) | `run_decision_pipeline`, `replay_decision_pipeline_run`                                                                  |
| `case_routing.py`         | Export taxonomy → operacyjny wiersz                    | `CaseRouting`, `desk_eligible`, `case_row_requires_action`, `route_from_classification`, `enrich_case_row_before_upsert` |
| `case_family_boundary.py` | Granica klient vs internal task                        | `INTERNAL_TASK_CASE_FAMILY`, `is_internal_task_row`, `ACTIVE_CUSTOMER_CASES_SQL_WHERE`                                   |

Formalna decyzja **nie** powinna żyć w samym LLM ani w UI — musi przejść przez `DecisionCandidate` + `PolicyDecision` + `ActionProposal` tam, gdzie dotyczy.

### Case linking i identity

| Moduł                      | Rola                                                    |
| -------------------------- | ------------------------------------------------------- |
| `case_linker.py`           | Główna heurystyka / reguły łączenia sygnału ze sprawą   |
| `case_identity.py`         | Tożsamość sprawy (klucze, normalizacja identyfikatorów) |
| `case_snapshot_manager.py` | `CaseSnapshotManager.apply_signal` — hot state, wersje  |
| `calendar_case_linker.py`  | Powiązanie zdarzeń kalendarza ze sprawą                 |
| `drive_case_linker.py`     | Powiązanie dokumentów Drive ze sprawą                   |

Regresje: zmiany w `case_identity` często psują backfill i `case-context`.

### Mailbox Memory / Postgres (pakiet `mailbox_memory/`)

Warstwa **operacyjnej prawdy**. Pełny schemat tabel — §3g.

| Plik                         | Odpowiedzialność                                                                                   |
| ---------------------------- | -------------------------------------------------------------------------------------------------- |
| `mailbox_memory/protocol.py` | `MailboxMemoryStore(Protocol)` — ~50 metod (fetch/upsert/append)                                   |
| `mailbox_memory/schema.py`   | DDL + helpery wektorowe (`build_mailbox_memory_vector_schema_sql`, `_cosine_similarity`)           |
| `mailbox_memory/postgres.py` | `PostgresMailboxMemoryStore` (~1462 L); `bootstrap()` łączy mailbox + registry + agent runtime SQL |
| `mailbox_memory/inmemory.py` | `InMemoryMailboxMemoryStore` — test double                                                         |
| `mailbox_memory_runtime.py`  | Ingest, pack, `rank_chunks`, hot state, embeddingi (`apply_embeddings_to_chunk_rows`)              |
| `mailbox_memory_models.py`   | `CaseContextPack`, `MailboxMemoryIngestResult`, `SignalJournalEntry`                               |
| `mailbox_memory_health.py`   | Proble/health-checki pod `doctor`                                                                  |

**Invariant:** Postgres jest źródłem prawdy; **pgvector** to helper retrieval (ranking może uwzględnić wektor, ale nie „definiuje faktu”).

### Vector retrieval / semantic ranking

Retrieval hybrydowy w `mailbox_memory_runtime.rank_chunks`:

```text
lexical_score  = |query_tokens ∩ chunk_tokens| / |query_tokens|
freshness_hint = 1.0 (jest timestamp) lub 0.5
retrieval_score = lexical_score*0.50 + vector_score*0.35 + freshness_hint*0.15   # gdy wektor dostępny
# gdy wektor brak: tylko lexical + freshness
```

Wektor przez pgvector: `fetch_semantic_chunk_candidates_for_case` używa dystansu `<=>` (cosine), `vector_similarity = GREATEST(0, LEAST(1, 1.0 - (embedding <=> query)))`, filtr `embedding_status='ready'`. Fallback in-memory: `_cosine_similarity`. To **dowód lokalny**; osobno żyje pytanie, czy deployment ma poprawnie skonfigurowany pgvector (`MAILBOX_MEMORY_VECTOR_ENABLED`).

### CaseContextPack / EvidenceRef / ContextQuality

- `case_context_contract.py` — kształt packa, helpery feedu, re-export `normalize_evidence_refs`; `case_context_deterministic.py` — deterministyczny merge konfliktów/luk.
- `evidence_ref.py` — `normalize_evidence_ref(s)`, role, trust/freshness, **zakazane klucze** (`excerpt`, `body`, …); surowe treści nie wchodzą do operator-facing dowodu; `can_answer_customer` ścisłe względem trust.
- `context_quality_contract.py` — jedno źródło kształtu `ContextQuality` (`ready_for_decision`, `operator_review_possible`, `action_readiness`, `not_ready_reasons`, `has_blocking_*`). To **gotowość kontekstu**, nie hub jakości produktu.

Granice LLM JSON: `intake_schema.validate_business_reasoning_result` czyści `evidence_refs`/`conflict_refs`; `document_intelligence_runtime` ma **osobną** powierzchnię pól dokumentu (np. `excerpt`) — nie mylić z EvidenceRef projekcji.

### Decision chain (gated)

| Moduł                     | Rola                                                                       | Flaga / uwaga                                                    |
| ------------------------- | -------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `understanding_output.py` | Projection-safe „sytuacja” (esencja, konflikty, ryzyka, NBA rec.)          | `UNDERSTANDING_OUTPUT_ENABLED` (default 0)                       |
| `decision_candidate.py`   | Pierwszy **formalny** obiekt „co proponujemy” + `context_quality_ref`      | produkowany gdy pipeline on; zero execution                      |
| `decision_pipeline.py`    | Orkiestracja klasyfikatorów → kandydat                                     | `DECISION_PIPELINE_ENABLED` (default 0)                          |
| `policy_engine.py`        | Twarda bramka (bez LLM, bez matematyki kalk-top)                           | **zawsze** przez `attach_policy_and_proposals`                   |
| `policy_decision.py`      | `PolicyReport` → P0 gate                                                   | `DECISION_PIPELINE_DRY_RUN_ONLY` (default 1; `full` profile = 0) |
| `action_proposal_v2.py`   | Propozycje **po** policy (`build_policy_gated_action_proposals_v2_bundle`) | `ACTION_PROPOSAL_V2_ENABLED` (default 0)                         |

**Punkt zaczepienia policy w runtime:** `policy_action_proposal.attach_policy_and_proposals()` — jedyne miejsce evaluate + attach na ścieżce shared downstream. `build_case_intelligence_layer` **nie** woła `evaluate_policy` bezpośrednio.

### Case Intelligence (`case_intelligence/`)

| Submoduł              | Kluczowa funkcja                                                                                        |
| --------------------- | ------------------------------------------------------------------------------------------------------- |
| `orchestrator.py`     | **`build_case_intelligence`**, `apply_hot_state_to_case_intelligence`                                   |
| `desk.py`             | **`build_desk_composition`**, `merge_case_guidance_into_intelligence`, `should_suppress_desk_and_tasks` |
| `risks.py`            | `build_risk_assessment`                                                                                 |
| `missing_info.py`     | `build_missing_info` (critical/important/helpful gaps)                                                  |
| `next_best_action.py` | `build_next_best_action`                                                                                |
| `understanding.py`    | `build_case_understanding_snapshot`, `build_case_operator_brief`                                        |
| `lifecycle.py`        | `build_merge_split_suggestions`, `build_feedback_learning_memory`, `build_lifecycle_revision`           |
| `validators.py`       | `validate_case_intelligence_result`                                                                     |
| `constants.py`        | Enumy: presence modes, action types, risk types                                                         |

### Agent runtime (`agent_runtime/`)

| Plik                       | Rola                                                                                       |
| -------------------------- | ------------------------------------------------------------------------------------------ |
| `graph.py`                 | Pętla agenta: plan → authz → tool → journal → HITL (`AgentGraphEngine.run`); timeouts      |
| `run.py`                   | `execute_agent_run` — load snapshot → engine → CAS save; gated `AGENT_RUNTIME_ENABLED`     |
| `orchestrator.py`          | Routing sygnału TUM (RFC E2): `route_signal` → fast_link/deep_understand/defer             |
| `materialize.py`           | Writer po HITL (`execute_materialize_proposal`, composite steps, engagement link)          |
| `tools/write_executors.py` | Zapisy po HITL (`execute_create_case`, `execute_merge_cases`, …)                           |
| `planner.py`               | `ToolPlanner` protocol; mocki dla testów                                                   |
| `openai_agent_client.py`   | `OpenAIToolPlanner.plan_next_tool` (circuit breaker, `tool_choice="auto"`)                 |
| `circuit_breaker.py`       | Per-provider izolacja awarii LLM                                                           |
| `business_pulse.py`        | `get_pipeline_summary` — `offers_in_progress` (COUNT aktywnych `lead_opportunity`/`sales`) |
| `authz.py`                 | operator/read auth oraz fail-closed `require_mutation_token` dla write routes               |
| `agent_hitl_bridge.py`     | stabilna tożsamość decyzji, trwały send result, replay-safe completion                       |
| `hitl_gmail_send.py`       | granica efektu: before-execution / effect-started / executed / outcome-unknown               |
| `execution_runtime.py`     | idempotentny approve/reject, stabilny finalny event i jawny konflikt                         |

### Signal journal, replay, reconciliation

| Plik                          | Rola / kluczowa funkcja                                                                     |
| ----------------------------- | ------------------------------------------------------------------------------------------- |
| `raw_observation_contract.py` | `RawObservation`, `build_observation_id`, `build_payload_hash`                              |
| `signal_contract.py`          | `CanonicalSignal`, `build_signal_id`, `build_idempotency_key`                               |
| `signal_journal.py`           | `SignalJournal` (append-only), `record_processing_attempt`, `fetch_signal`                  |
| `signal_worker.py`            | `run_signal_loop` (pętla), `replay_signal_from_journal`, `rebuild_case_from_signal_journal` |
| `signal_reconciler.py`        | `reconcile_signal`, `reconcile_signal_batch`, `replay_signal`                               |
| `case_state_rebuilder.py`     | `case_rebuild_from_journal`, `incremental_refresh`, `projection_only_refresh`               |

Cel: audyt i replay bez „magicznego stanu tylko w UI”.

### Drive / document intelligence, Calendar, LLM routing, Eval

- **Drive** (drugi ingress do wspólnego substratu): `drive_ingest_runtime.py`, `drive_client.py`, `drive_case_linker.py`, `drive_lane_classifier.py`, `document_intelligence_runtime.py`, `document_intelligence_contract.py`.
- **Calendar**: `calendar_client.py`, `calendar_runtime.py`, `calendar_signal_adapter.py`, `calendar_case_linker.py` — oś czasu jako kontekst/sygnał; live write poza ingest wymaga osobnego scope.
- **LLM routing** — patrz §3d.
- **Eval / quality** (read-only analytics, bez mieszania z truth): `feedback_event_contract.py`, `feedback_analytics_export.py`, `eval_shadow_analytics.py`, `quality_readonly_projection.py`, `quality_readonly_integration.py`, `ai_quality_runtime.py`, `confidence_review.py`, `confidence_calibration.py`.

## 3a. Biblioteki Python (`tools/gmail_audit/requirements.txt`)

| Pakiet                                           | Rola w tym repo                                                                  |
| ------------------------------------------------ | -------------------------------------------------------------------------------- |
| `requests`, `httpx`                              | HTTP klienci (Groq Responses, Google, Daszek bridge, OTLP)                       |
| `pydantic`                                       | Modele kontraktów / walidacja                                                    |
| `python-dotenv`                                  | wczytywanie `.env`                                                               |
| `jsonschema`                                     | walidacja payloadów / kontraktów                                                 |
| `fastapi`, `uvicorn[standard]`                   | Node B HTTP API (`api_app.py`) + ASGI server                                     |
| `google-auth`                                    | OAuth / tokeny do API Google                                                     |
| `opentelemetry-api/sdk/exporter-otlp-proto-http` | telemetria (eksport OTLP HTTP)                                                   |
| `pypdf`, `Pillow`, `pytesseract`                 | ekstrakcja treści i OCR w torze dokumentów                                       |
| `psycopg[binary]`, `pgvector`                    | Postgres jako pamięć operacyjna; rozszerzenie wektorowe                          |
| `neo4j`                                          | driver pilota grafowego (sprawdź, czy feature aktywny — ≠ „jest w requirements”) |
| `openai`                                         | Agent planner (Chat Completions + tool_calls)                                    |
| `mcp`                                            | serwer MCP stdio (`agent-mcp-serve`, debug)                                      |
| `pytest`                                         | testy w `tools/gmail_audit/tests/` (obecny też w obrazie prod)                   |

**Docling** jest opcjonalny — **nie** w `requirements.txt`, instalowany przez Docker build arg `INSTALL_DOCLING=1`. Nowa biblioteka produkcyjna = **PR musi zawierać** aktualizację `requirements.txt` + uzasadnienie; ten README aktualizuj gdy zmienia się rola widoczna dla developera.

## 3b. Zmienne środowiska (gdzie szukać — bez wartości)

Źródło prawdy: `tools/gmail_audit/config.py` (`os.getenv`, `Settings`, `@dataclass(slots=True)`). **Ładowanie:** pierwszy istniejący: `GMAIL_AGENT_ENV_FILE` → `tools/gmail_audit/.env` → **`<repo-root>/.env`** (`load_dotenv(override=False)`). `tools/gmail_audit/.env.local` **nigdy** nie jest ładowany (warning). `.env.local-vps` używany przez Docker (`--env-file`). Pełna tabela: [`docs/dev/ENV_LOADING.md`](../dev/ENV_LOADING.md).

| Grupa                              | Przykładowe zmienne (nazwa dokładnie jak w kodzie)                                                                                                                                                                                              |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Profil / DB / blob                 | `GMAIL_AGENT_RUNTIME_PROFILE`, `CASE_OS_RUNTIME_PROFILE`, `EMERGENCY_INTELLIGENCE_KILLSWITCH`, `MAILBOX_MEMORY_DATABASE_URL`, `DATABASE_URL`, `MAILBOX_MEMORY_BLOB_ROOT`, `MAILBOX_MEMORY_STAGE_MODE`                                           |
| Google OAuth / Gmail               | `GOOGLE_ACCESS_TOKEN`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `GOOGLE_TOKEN_ENDPOINT`, `GOOGLE_OAUTH_SCOPES`                                                                                                       |
| Drive                              | `GOOGLE_DRIVE_ENABLED`, `GOOGLE_DRIVE_CREDENTIALS_PATH`, `GOOGLE_DRIVE_SHARED_DRIVE_ID`, `GOOGLE_DRIVE_ROOT_FOLDER_ID`, `GOOGLE_DRIVE_INGEST_ENABLED`, `GOOGLE_DRIVE_GRAPH_ENABLED`                                                             |
| Calendar                           | `GOOGLE_CALENDAR_ENABLED`, `GOOGLE_CALENDAR_ID`                                                                                                                                                                                                 |
| LLM chat (structured intake)       | `LLM_BACKEND`, `LLM_PRIMARY_PROVIDER`, `LLM_FALLBACK_PROVIDERS`, `LLM_STRUCTURED_PROVIDER_ALTERNATION`, `GROQ_*`, `OPENAI_COMPAT_*`, `CEREBRAS_*`, `NVIDIA_*`, `ANTHROPIC_*`, `CASE_GUIDANCE_*`                                                 |
| Agent planner (LLM agenta)         | `AGENT_CEREBRAS_*`, `AGENT_NVIDIA_*`, `AGENT_GROQ_*`, `AGENT_OPENAI_API_KEY`/`AGENT_OPENAI_BASE_URL` (OpenRouter), `AGENT_MODEL_FALLBACK`, `AGENT_OPENAI_NATIVE_API_KEY`, `AGENT_CURSOR_API_KEY`                                                |
| Embeddingi (retrieval)             | `MAILBOX_MEMORY_VECTOR_ENABLED`, `OPENAI_COMPAT_EMBEDDING_MODEL`, `OPENAI_COMPAT_EMBEDDING_DIMENSIONS`, `OPENAI_COMPAT_EMBEDDING_BASE_URL`, `OPENAI_COMPAT_EMBEDDING_API_KEY`                                                                   |
| Daszek (Node B → Node A push)      | `DASZEK_BASE_URL`, `DASZEK_LOGIN`, `DASZEK_PASSWORD`, `DASZEK_BRIDGE_TOKEN`, `DASZEK_V2_PUSH`, `DASZEK_OPERATIONAL_FEED_*`, `DASZEK_FEED_SOURCE`                                                                                                |
| Daszek → Node B API (Skrzat/agent) | `DASZEK_NODE_B_API_BASE`, `DASZEK_NODE_B_API_TOKEN` — konfiguracja **WordPress/Daszek** (`../daszek/includes/config.php`), nie `config.py`                                                                                                      |
| Node B internal auth (registry)    | `NODE_B_REGISTRY_TOKEN`, `GMAIL_AGENT_INTERNAL_API_TOKEN` (+ `DASZEK_NODE_B_API_TOKEN`) — bearer dla `/internal/*` i HITL/materialize                                                                                                           |
| Neo4j pilot                        | `NEO4J_PILOT_ENABLED`, `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`                                                                                                                                                        |
| Telemetry                          | `GMAIL_AGENT_OTEL_ENABLED`, `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`                                                                                                                                    |
| Signal / poll                      | `SIGNAL_RUNTIME_MODE` (tylko `active`), `SIGNAL_WORKER_ENABLED`, `GMAIL_INGRESS_OWNER`, `GMAIL_HISTORY_POLL_INTERVAL_SEC`, `DRIVE_CHANGES_POLL_INTERVAL_SEC`, `EVENT_SPINE_PROCESSOR_*`                                                         |
| Case OS flags (gated pipeline)     | `UNDERSTANDING_OUTPUT_ENABLED`, `DECISION_PIPELINE_ENABLED`, `DECISION_PIPELINE_DRY_RUN_ONLY`, `ACTION_PROPOSAL_V2_ENABLED`, `CASE_INTELLIGENCE_VNEXT_ENABLED`, `INTELLIGENCE_SHADOW_PROJECTION`, `AGENT_RUNTIME_ENABLED`, `AGENT_RUNTIME_MODE` |
| HTTP ogólne                        | `HTTP_TIMEOUT`, `HTTP_MAX_RETRIES`, `HTTP_RETRY_BASE_DELAY`                                                                                                                                                                                     |
| Docling (limity)                   | `DOCLING_MAX_PAGES`, `DOCLING_TIMEOUT_SEC`                                                                                                                                                                                                      |

**Profile:** `GMAIL_AGENT_RUNTIME_PROFILE` — `""`/`default`/`slice` lub `canonical_production` (strict violations). `CASE_OS_RUNTIME_PROFILE` — `full` (default; flagi→1) lub `minimal` (flagi→0). `AGENT_RUNTIME_MODE=primary` jest **trwale odrzucany** (`validate_agent_runtime_mode_not_primary`).

Nie duplikuj tutaj sekretów ani przykładowych wartości — patrz sekcja 13.

## 3c. Wybrane łańcuchy zależności (producent → konsument)

Skrót **logiczny** (nie pełny graf importów). Importy `from X import Y` zmieniają się częściej niż semantyka — przy refaktorze aktualizuj testy, nie tylko tabelę.

| Producent / właściciel                                   | Konsumenci (przykłady)                                                                                  | Uwaga                                                               |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| `evidence_ref.py`                                        | `case_context_contract`, `decision_projection_blocks`, `intake_schema`, `case_intelligence/`            | pojedynczy canonical dla list EvidenceRef                           |
| `case_context_contract` + `case_context_deterministic`   | `mailbox_memory_runtime`, `understanding_output`, pack builders                                         | pack + merge konfliktów/luk                                         |
| `case_write_gateway`                                     | `api_app` (`/tasks*`, `/cases`), reconciler i ścieżki manualne                                          | full writes przez `write_case_row`; częściowe równoległe mutacje przez `mutate_case` |
| `case_routing`                                           | `case_write_gateway`, `daszek_v3_operational_feed`, `business_pulse`, `api_app`                         | `requires_action`, `desk_eligible`, `enrich_case_row_before_upsert` |
| `correlation_registry.service`                           | `api_app` (`/internal/registry/links`, `/cases/{id}/engagement`), `case_engagement_bridge`, materialize | identity/engagement/links                                           |
| `intake_shared_downstream.run_shared_downstream_stages`  | `_reconcile_gmail_signal`, `_reconcile_drive_signal`                                                    | wspólny ogon reconcile                                              |
| `policy_action_proposal.attach_policy_and_proposals`     | `run_shared_downstream_stages`                                                                          | jedyne policy attach                                                |
| `projection_snapshot_transport`                          | `build_v2_projection`, reconcile, `api_app`                                                             | jeden operator snapshot (v2 + decision_view + trays)                |
| `case_intelligence.orchestrator.build_case_intelligence` | `build_case_intelligence_layer` (`gmail_intake`), projekcja, proof-packi                                | scala lifecycle/missing/risks/NBA/understanding/desk                |
| `daszek_v3_operational_feed` + `..._contract`            | `daszek_v3_feed_runtime`, `daszek_client`, testy feedu                                                  | schema 1.3, forbidden keys, walidacja                               |
| pakiet `mailbox_memory/`                                 | intake, context pack, ranking, health, `case_write_gateway`                                             | Postgres truth; vector helper                                       |
| `signal_journal` + `signal_worker`                       | replay, `signal_reconciler`, `case_state_rebuilder`                                                     | append-only, deterministyczny replay                                |

Kontrakt dowodów: `docs/core/evidence_ref_contract.md`.

## 3d. LLM routing (dwa niezależne łańcuchy)

Node B ma **dwa** rozłączne łańcuchy providerów:

**1. Structured intake (`groq_client.py` + `llm_provider_router.py`)** — walidowany JSON dla business reasoning / intake:

- `request_structured_output` → `LLMRouter(_structured_providers(...))`.
- Plan: `(LLM_PRIMARY_PROVIDER, *LLM_FALLBACK_PROVIDERS)`; z alternacją dodaje slot groq/cerebras.
- Providerzy: `groq` (Responses API + json_schema), `openai_chat`, `cerebras`, `nvidia`.
- Domyślny `.env.example`: `LLM_PRIMARY_PROVIDER=groq`, `LLM_FALLBACK_PROVIDERS=cerebras,nvidia,openai_chat`.
- Fallback **tylko** na błędy retryable (`rate_limit`, `quota_exhausted`, `server_error`, `timeout`, `network`); auth/config/contract → brak fallbacku. 402/429 → szybki fallback. Metadane: `llm_selected_provider`, `llm_fallback_used`, `llm_fallback_reason`.

**2. Agent planner (`agent_runtime/settings.py` → `openai_agent_client.py`)** — pętla narzędziowa agenta. `build_agent_planner_endpoints()` — łańcuch (decyzja operatora 2026-07-07):

1. **Cerebras** (`AGENT_CEREBRAS_*` / `CEREBRAS_*`)
2. **NVIDIA** (`AGENT_NVIDIA_*` / `NVIDIA_*`, default `meta/llama-3.3-70b-instruct`)
3. **Groq** (`AGENT_GROQ_*` / `GROQ_*`)
4. **OpenRouter** (`AGENT_OPENAI_API_KEY` + `AGENT_OPENAI_BASE_URL`)
5. tail opcjonalny: native OpenAI, Cursor.

`OpenAIToolPlanner.plan_next_tool` iteruje endpointy z circuit breakerem + backoff na 429/timeout. **`tool_choice="auto"`** (Groq nie wspiera `required`). Twardy timeout 45 s (executor), 30 s (client).

**Zasada:** LLM to narzędzie klasyfikacji/draftu/planowania; **nie** źródło prawdy o stanie sprawy.

## 3e. Operational Feed v3 (schema 1.3)

Kontrakt: `daszek_v3_operational_feed_contract.py` (`OPERATIONAL_FEED_SCHEMA_VERSION_LATEST = "1.3"`, `validate_operational_feed_snapshot`, `FORBIDDEN_KEYS_ANYWHERE`). Builder: `daszek_v3_operational_feed.py` (`build_operational_feed_from_mailbox_store`, `build_operational_feed_for_cel`).

**Listy w obiekcie `feed`:** `desk`, `cases`, **`action_items`** (wymagane w 1.3), `case_details` (mapa), `day` (`sections`), opcjonalnie `quality_readonly`.

**Zmiana 1.2 → 1.3:** brak listy `tasks`. `_emit_action_items` emituje tylko `action_items`; `_dual_emit_action_items` deleguje do niego gdy `SCHEMA_VERSION >= "1.3"`. Kontrakt **ostrzega**, jeśli w payloadzie pojawi się `feed.tasks`.

**`feed_meta`:** `exporter`, `contract_module`, `state_freshness`, `desk_filter` (`P1_P2_operational`), `action_items_label`, `cases_in_feed_count`, `desk_case_count`, `operational_case_count`, `desk_eligible_count`, `engagement_only_staging_count`, `ui_sources` (`biurko`→`feed.desk`, `sprawy`→`node_b_GET_cases`).

**Envelope root:** `schema_name: daszek_operational_feed_snapshot`, inwarianty `read_only`/`creates_cases`/`executes_actions`, `counts`, `source`, pola trust (`environment`, `source_run_id`, `build_git_sha`).

**Zakazane klucze wszędzie** (`FORBIDDEN_KEYS_ANYWHERE`): `email_body`, `body`, `snippet`, `subject`, `raw_llm`, … — feed nie przenosi surowych treści.

**Push:** `daszek_v3_feed_runtime.maybe_push_operational_feed_from_run_state()` (debounce, cache 30 s) → `daszek_client.post_v3_operational_feed_snapshot()` → `POST /wp-json/daszek/v3/operational-feed-snapshots` (bridge token lub login+CSRF; `DaszekCircuitBreaker`). Polityka: `daszek_push_policy.evaluate_live_push_policy`.

## 3f. Powierzchnia HTTP API (Node B — `api_app.py`)

FastAPI `create_app()` — Node B API. Route’y read i write mają różne kontrakty auth. Nie zakładaj, że brak tokenu oznacza tryb developerski.

- `/internal/*`, HITL/materialize i chronione mutation routes używają bearer envelope;
- `/tasks*` write routes są fail-closed przez `require_mutation_token()`;
- brak, błędny lub read-only credential kończy się przed walidacją biznesową i store;
- `/agent-chat*` używa operator bearer + rate limit;
- szczegółowe wymagania route potwierdzaj w `api_app.py` i testach auth.

### System / health

`GET /health`, `GET /system/trace`, `GET /system/worker/health`, `GET /system/health/status`, `GET /system/agent-health`, `GET /system/decision-queue`, `GET /system/briefing`, `GET /system/cost-summary`, `GET /system/quality-summary`, `GET /system/constitution`, `GET /system/similar-families`, `POST /system/patterns/discover`, `GET /system/os-events/recent`.

### Cases (read + context)

`GET /cases` (lista spraw klienckich; params `requires_action`, `case_family`, `source_kind`, `desk_only`, `view=actionable|informational`, `limit`), `GET /cases/{id}/context-pack`, `/evidence`, `/conflicts`, `/gaps`, `/context-trays`, `/state-summary`, `GET /cases/{id}/attachments/{ref}` (registry bearer jeśli skonfigurowany), `GET /cases/{id}/engagement`, `GET /cases/{id}/offers/latest`, `POST /cases/{id}/operator-action`, `GET /cohort-runs/{run_id}`.

`GET /cases/{id}/offers/latest` jest read-only projekcją Case OS z `unified_os_events`. Node B nie przejmuje `OfferDTO`: `kalk-top` pozostaje właścicielem pricing/sizing/OfferDTO, `top-instal-generator` właścicielem dokumentu, a `gmail-agent` przechowuje tylko kanoniczną obserwowalność i provenance (`offer.generated` / `offer.status_updated`) powiązane z Case/Engagement.

### Engagements / materialize / HITL

`GET /engagements/{id}/timeline`, `/os-events`, `/snapshot`, `POST /engagements/{id}/hitl/approve` (registry bearer), `POST /engagements/{id}/materialize/approve` (registry bearer + idempotency key).

### Identity

`GET /identity/suggestions`, `GET /identity/binding-suggestions[/{id}]`, `POST /identity/binding-suggestions/scan`, `POST /identity/binding-suggestions/{id}/status` (approve→merge). **Deprecated:** `GET /identities/by-email/{email}`, `POST /identity/merge` → **410 Gone**.

### Tasks (deprecated list; zapisy chronione)

`GET /tasks` pozostaje deprecated read shim. Mutacje `POST /tasks` oraz `POST /tasks/{id}/confirm|reject|done`:

- wymagają write credential w modelu fail-closed;
- nie docierają do store po nieautoryzowanym requestcie;
- tworzą/aktualizują Case przez kanoniczny gateway;
- częściowe update istniejącego Case korzystają z atomowego `mutate_case`;
- odpowiedź auth nie ujawnia istnienia Case przed autoryzacją.

### Agent-chat

`POST /agent-chat` (sync reconcile + proposals; bearer + rate-limit), `POST /agent-chat/stream` (SSE), `POST /agent-chat/feedback` (thumbs).

### Skrzat / Learning / Business dictionary

`POST /cases/{id}/skrzat/ask`; `GET /learning/rule-candidates`, `POST /learning/rule-candidates/{id}/status`; `GET /business-dictionary/terms`, `GET /business-dictionary/stats`.

### Internal (cross-repo, registry bearer wymagany)

`POST /internal/os-events` (publikacja OS event + hook Cieplo), `POST /internal/registry/links` (rejestracja correlation links), `POST /internal/email/personalize-offer`.

Offer visibility korzysta z istniejącego `POST /internal/os-events`: producenci publikują `offer.generated` oraz opcjonalne `offer.status_updated` z `case_id`, `offer_id`, `source`, timestampem, ceną/model/document reference/status i provenance. Brak jednoznacznego `case_id` albo `engagement_id` failuje jawnie; retry tego samego offer nie tworzy drugiej obserwacji.

## 3g. Schemat bazy (Postgres)

DDL w `mailbox_memory/schema.py` (`MAILBOX_MEMORY_SCHEMA_SQL`), registry w `correlation_registry/schema.py`, agent runtime w `agent_runtime/AGENT_RUNTIME_SCHEMA.sql` + `LEARNING_LOOPS_MIGRATIONS.sql`. `PostgresMailboxMemoryStore.bootstrap()` aplikuje je łańcuchowo.

**Mailbox memory (rdzeń):**

| Tabela                                                                                                 | Klucz / wybrane kolumny                                                                                                                                                                                                                                                                             |
| ------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mailbox_memory_cases`                                                                                 | `case_id`; `case_key`, `thread_id`, `case_family`, `mailbox`, `subject`, `status`, `customer_name`, `customer_email`, `latest_signal_id`, `latest_signal_at`, `last_rebuild_at`, `last_projection_refresh_at`, `last_source_kinds_seen` (jsonb), **`metadata` (jsonb)**, `created_at`, `updated_at` |
| `mailbox_memory_messages`                                                                              | `message_id`; `case_id`, `thread_id`, `sender_email`, `subject`, `snippet`, `body_text`, `labels`, `received_at`, `raw_snapshot`                                                                                                                                                                    |
| `mailbox_memory_attachments`                                                                           | `attachment_id`; `case_id`, `message_id`, `file_name`, `mime_type`, `content_sha256`, `blob_path`                                                                                                                                                                                                   |
| `mailbox_memory_documents`                                                                             | `document_id`; `case_id`, `attachment_id`, `document_kind`, `extraction_status`, `text_content`, `summary_text`                                                                                                                                                                                     |
| `mailbox_memory_document_chunks`                                                                       | `chunk_id`; `document_id`, `case_id`, `ordinal`, `chunk_text`, `embedding_status`, **`embedding vector(dim)`** (HNSW cosine)                                                                                                                                                                        |
| `mailbox_memory_facts`                                                                                 | `fact_id`; `case_id`, `entity_scope`, `fact_key`, `normalized_value`, `confidence`, `status`                                                                                                                                                                                                        |
| `mailbox_memory_events`                                                                                | `event_id`; `case_id`, `event_type`, `occurred_at`, `payload`, `source_refs`                                                                                                                                                                                                                        |
| `mailbox_memory_snapshots` / `_case_snapshot_versions`                                                 | snapshot sprawy + wersje (`snapshot_json`)                                                                                                                                                                                                                                                          |
| `mailbox_memory_next_actions`                                                                          | `case_id`; `next_action`, `rationale`, `source_stage`                                                                                                                                                                                                                                               |
| `mailbox_memory_action_proposals`                                                                      | `proposal_id`; `case_id`, `action_type`, `risk_class`, `status`, `policy_basis`                                                                                                                                                                                                                     |
| `mailbox_memory_execution_results`                                                                     | `execution_id`; `proposal_id`, `case_id`, `execution_status`, `policy_result`                                                                                                                                                                                                                       |
| `mailbox_memory_raw_observations` / `_signals` / `_signal_processing_attempts`                         | spine sygnałów (`idempotency_key` unique, `engagement_id`)                                                                                                                                                                                                                                          |
| `mailbox_memory_source_cursors`                                                                        | `cursor_key`; `source_kind`, `last_cursor`                                                                                                                                                                                                                                                          |
| `mailbox_memory_calendar_events` / `_calendar_case_links`                                              | linkowanie kalendarza                                                                                                                                                                                                                                                                               |
| `mailbox_memory_document_intelligence_results` / `_extracted_fields` / `_conflicts`                    | DI pipeline                                                                                                                                                                                                                                                                                         |
| `company_drive_documents` / `_document_chunks` (+vector) / `company_drive_facts` / `drive_ingest_runs` | tor Drive                                                                                                                                                                                                                                                                                           |

**Correlation registry:** `topinstal_identities`, `topinstal_engagements`, **`correlation_links`** (`link_id`, `engagement_id`, `link_type`, `target_id`, `source_repo`, `confidence`; unique `(link_type, target_id, source_repo)`), `unified_os_events`, `event_spine_handler_effects`, `identity_binding_suggestions`, `identity_merge_log`.

**Agent / learning:** `operator_engagement_snapshots`, `agent_runtime_turns`, `agent_run_checkpoints`, `agent_proposal_records`, `operator_response_records`, `learning_rule_candidates`, `historical_corpus_messages/facts`, `world_model_insights`.

> **Uwaga:** feedback operatora nie ma osobnej tabeli `mailbox_memory_feedback` — żyje w `mailbox_memory_events` / modułach operatorskich.

**Execution safety:** implementacja korzysta z istniejących execution records i/lub trwałego metadata Case keyed by stabilny `decision_key`. Dokumentuj semantykę na poziomie kontraktu, nie zakładaj osobnej tabeli, jeśli kod jej nie tworzy. Źródłem prawdy jest aktualny store i testy `agent_hitl_bridge`/`execution_runtime`.

## 4. Czego aplikacja nie robi i nie powinna robić bez osobnego sprintu

**Ogrodzenie oczekiwań:** brak pozycji na liście nie oznacza „feature włączony na produkcji”, a obecność kodu nie oznacza zgody biznesowej na skutek uboczny (np. wysyłka maila).

- **Autonomiczna wysyłka maili do klientów** — execution outbound jest osobną ścieżką; draft ≠ send.
- **Automatyczne umawianie serwisu** — wymagałoby write kalendarzowego + polityk.
- **Live zapis Calendar** — ingest read ≠ write.
- **Zapis do CRM** — poza repo lub za adapterem.
- **Finalne oferty HVAC, pricing, sizing, marża** — właściciel: `kalk-top` / generator dokumentów, nie Node B.
- **OfferDTO jako źródło prawdy w `gmail-agent`** — brak; tylko referencje.
- **Gate B / 24h production proof „z definicji”** — wymaga runbooka i artefaktu.
- **„Pgvector działa na produkcji” z testów lokalnych** — testy dowodzą modelu rankingu, nie konfiguracji deploymentu.

### Właściciele zewnętrzni (granice odpowiedzialności)

| System                            | Co jest jego prawdą                                                |
| --------------------------------- | ------------------------------------------------------------------ |
| `kalk-top`                        | HVAC, sizing, pricing, marża, `CalcRequestDTO → OfferDTO`          |
| `top-instal-generator`            | rendering DOCX/PDF z danych oferty                                 |
| WordPress / Daszek / `wp-adapter` | operator UI, bridge, część transportu; nie semantyka policy Node B |
| `topinstal-mail-ingress`          | cienki adapter wejścia maili; **nie** zastępuje intake ani pamięci |

## 5. Topologia fizyczna (deployment lokalny / Docker)

**Stan 2026-07:** system działa **lokalnie na Docker Compose**; produkcyjny VPS jest **zawieszony**. `docker-compose.vps.yml` istnieje jako historyczny/produkcyjny szablon.

```text
Node A: WordPress / Daszek / operator UI            (lokalnie: kontener WP / stack Daszka)
Node B: gmail-agent / Python / Postgres / pgvector  (lokalnie: docker-compose.local-vps.yml)
```

### `gmail-agent/docker-compose.local-vps.yml`

| Serwis                  | Obraz / build                       | Port (host → kontener)                                                                                          |
| ----------------------- | ----------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `mailbox-memory-db`     | `pgvector/pgvector:pg16`            | `127.0.0.1:${MAILBOX_MEMORY_PG_PORT:-54129}:5432`                                                               |
| `neo4j`                 | `neo4j:5-community`                 | 7474 / 7687 (localhost)                                                                                         |
| `ollama`                | `ollama/ollama:latest`              | `:11434` (embeddingi lokalne)                                                                                   |
| `gmail-agent-nodeb-api` | build → `gmail-agent-runtime:local` | profile `api`, `${GMAIL_AGENT_NODEB_PORT:-8765}` → 8765 (lokalnie zwykle **8766** gdy 8765 zajęte przez Cursor) |
| `gmail-agent-worker`    | ten sam obraz                       | profile `worker` (default `doctor --skip-gmail`)                                                                |

**Obraz** (`docker/gmail-audit.Dockerfile`): baza `python:3.12-slim`; build args `INSTALL_DOCLING` (default 0), `INSTALL_PHP` (default 0); system: tesseract-ocr (+pol), libgl1, curl; instaluje `requirements.txt`. Mount `.env.local-vps` → `/etc/topinstal/gmail-agent.env`.

**Częste pułapki:**

- lokalny folder `../daszek/` w repo to kod referencyjny/dev, nie produkcyjny storage Node A;
- Node B nie zakłada dostępu do `wp-content/uploads` bez wyraźnej integracji;
- lokalne testy nie dowodzą stanu deploymentu ani env;
- proof „live” wymaga artefaktu (`runs/`, log, `LAST_PROVEN_STATE`) — nie „przechodzi u mnie”.

Szczegóły: `docs/core/PHYSICAL_TOPOLOGY.md`.

## 6. Statusy prawdy

Rozdzielaj **poziom dowodu**, żeby uniknąć „documentation drift → fałszywa pewność”:

| Status                            | Znaczenie                                |
| --------------------------------- | ---------------------------------------- |
| `confirmed locally`               | działa w lokalnym repo/testach           |
| `confirmed by local tests`        | ma test lokalny                          |
| `confirmed on Node B (docker)`    | sprawdzone w lokalnym kontenerze Node B  |
| `confirmed on WordPress / Node A` | sprawdzone po stronie WordPress/Daszka   |
| `operator verified`               | operator potwierdził przepływ            |
| `historical`                      | dotyczy starego artefaktu (np. VPS)      |
| `not proven`                      | nie wolno tego claimować jako działające |

Zanim napiszesz „działa”, przypisz claim do statusu. Główne źródło proofu runtime: `docs/runbooks/LAST_PROVEN_STATE.md`.

## 7. Jak zacząć jako developer

### Krok 1 — Knowledge Spine

1. root `AGENTS.md`;
2. `knowledge/INDEX.md`;
3. `knowledge/world-state.yaml` i `knowledge/source-of-truth.md`;
4. `knowledge/memory/OPERATOR_DECISIONS.md`;
5. `knowledge/memory/ACTIVE_WORKSPACE.md` i `LAST_SESSION.md`;
6. repo `AGENTS.md`;
7. `docs/core/CONSTITUTION_V2_1.md`;
8. ten README i właściwy kod/test.

Nie przywracaj repo-local `memory-bank`, historycznych proof-packów ani usuniętych handoffów jako aktywnego onboarding.

### Krok 2 — wybór ścieżki

- **Node B Python runtime:** ten README, `AGENTS.md`, właściwy moduł z §10 i targeted tests.
- **Runtime / proof:** `docs/runbooks/LAST_PROVEN_STATE.md`.
- **Persistence/concurrency:** `docs/runbooks/MAILBOX_MEMORY_POSTGRES.md`.
- **Node A / Daszek:** `../../../daszek/docs/core/PROJECT_README.md`.
- **Architektura:** `docs/core/CONSTITUTION_V2_1.md`, `ARCHITECTURE_PRECEDENCE.md`, `PHYSICAL_TOPOLOGY.md`.
- **Signal spine:** `docs/runbooks/SIGNAL_ACTIVE_ONLY.md`.

### Krok 3 — pierwszy kod do czytania wg typu zadania

| Zadanie                      | Zacznij od                                                                              |
| ---------------------------- | --------------------------------------------------------------------------------------- |
| zmiana kontraktu JSON / pack | `case_context_contract.py`, `evidence_ref.py`, `test_case_context_contract.py`          |
| zmiana copy operatora        | `decision_projection_blocks.py`, `action_semantics_glossary.md`                         |
| zmiana policy                | `policy_engine.py`, `policy_decision.py`, testy inwariantów v2                          |
| zapis sprawy / task          | `case_write_gateway.py`, `case_routing.py`, `test_case_write_gateway.py`                |
| korelacja / engagement       | `correlation_registry/`, `case_engagement_bridge.py`, `test_correlation_registry_p0.py` |
| feed operacyjny              | `daszek_v3_operational_feed.py`, `..._contract.py`                                      |
| nowa komenda CLI             | `gmail_intake_parser.py` (`add_parser`), `gmail_intake.py` (dispatch)                   |

## 8. Lokalna walidacja

Pytanie: „czy moja zmiana nie rozbiła kontraktów ani składni, zanim pójdę na deployment?”.

### Baseline (szeroki smoke)

```powershell
python -m compileall tools/gmail_audit scripts -q
python -m pytest tools/gmail_audit/tests -q --tb=line
node --check ../daszek/public/app.js
powershell -NoProfile -ExecutionPolicy Bypass -File ..\scripts\verify-local-gates.ps1
```

Ostatni proof 2026-07-13 jest zielony i opisany w LPS. Po zmianie chronionego runtime nie cytuj starego wyniku — wykonaj świeżą regresję. Jeśli kod jest bake’owany w API/workerze, test hostowy nie zastępuje rebuild/recreate i parity.

### Preflight / doctor / CLI

```powershell
python tools/scripts/agent_preflight.py
python tools/gmail_audit/gmail_intake.py --help
python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --verbose
python tools/gmail_audit/gmail_intake.py doctor --check-drive --verbose
python tools/gmail_audit/gmail_intake.py doctor --check-daszek --verbose
```

### Węższe testy (szybka pętla)

```powershell
python -m pytest tools/gmail_audit/tests/test_evidence_ref.py -q
python -m pytest tools/gmail_audit/tests/test_case_context_contract.py -q
python -m pytest tools/gmail_audit/tests/test_decision_projection_blocks.py -q
python -m pytest tools/gmail_audit/tests/test_case_write_gateway.py -q
python -m pytest tools/gmail_audit/tests/test_correlation_registry_p0.py -q
```

### Spine + Context Projection

```powershell
python -m pytest tools/gmail_audit/tests/test_process_snapshot_runtime_spine.py -q
python -m pytest tools/gmail_audit/tests/test_truth_flow_pr4_pr8.py -q
python -m pytest tools/gmail_audit/tests/test_context_tray_set.py tools/gmail_audit/tests/test_projection_snapshot_transport.py -q
python tools/scripts/context_projection_smoke.py
```

## 9. Główne katalogi

| Ścieżka                    | Rola szczegółowa                                                                                                          |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `tools/gmail_audit/`       | Cały backend Node B: intake, memory, policy, projekcja, bridge, registry. Import CLI zakłada `PYTHONPATH` na ten katalog. |
| `tools/gmail_audit/tests/` | Testy kontraktowe; fixture + monkeypatch, **nie** produkcyjny env.                                                        |
| `../daszek/`               | Statyczny/dev UI operatora; synchronizacja copy z `decision_projection_blocks.py` przy zmianach PL.                       |
| `../wp-bridges/`           | PHP/WordPress — transport i adaptery; semantyka decyzji zostaje w Node B.                                                 |
| `docs/core/`               | Konstytucja, topologia, kontrakty, katalog eventów i manuale architektury.                                                |
| `docs/dev/`                | Flagi runtime, ENV loading, skany GitNexus, handoffy.                                                                     |
| `docs/runbooks/`           | Operacje, proof, handoff; **jedyny** katalog na twierdzenia „proven” z datą i procedurą.                                  |
| zewnętrzne katalogi proof | Artefakty runtime są zapisywane poza repo i referencjonowane w LPS.                                                        |
| `../knowledge/memory/`     | Kanoniczna pamięć workspace; repo-local memory bank został usunięty.                                                       |
| `.agents/skills/`          | Procedury taskowych agentów (policy, memory pack, proof run, …).                                                          |
| `.cursor/rules/`           | Cienkie reguły IDE — nie duplikują `AGENTS.md`.                                                                           |
| `support/`                 | Skrypty pomocnicze repo (np. audit context pack).                                                                         |
| `scripts/`                 | Skrypty narzędziowe poza `gmail_audit` (compileall obejmuje ten katalog).                                                 |

## 10. Mapa modułów Node B

| Obszar                      | Pliki startowe                                                                                                                                                                                                                                    |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CLI/runtime                 | `gmail_intake.py`, `gmail_intake_parser.py`, `gmail_intake_process.py`, `gmail_intake_doctor.py`, `config.py`, `runtime_imports.py`                                                                                                               |
| Gmail                       | `gmail_auth.py`, `google_gmail_api.py`, `gmail_fetch.py`, `gmail_change_detector.py`, `gmail_signal_adapter.py`                                                                                                                                   |
| Intake schema               | `intake_schema.py`, `intake_payload.py`, `intake_second_pass.py`, `intake_policy.py`                                                                                                                                                              |
| Triage / routing            | `preclassifier.py`, `mail_classification.py`, `topic_classifier.py`, `case_type_classifier.py`, `priority_sla_scorer.py`, `case_routing.py`, `case_family_boundary.py`                                                                            |
| Case identity               | `case_linker.py`, `case_identity.py`, `case_snapshot_manager.py`, `case_snapshot_hot_state_contract.py`                                                                                                                                           |
| Memory (pakiet)             | `mailbox_memory/` (`protocol`, `schema`, `postgres`, `inmemory`, `facts`), `mailbox_memory_runtime.py`, `mailbox_memory_models.py`, `mailbox_memory_health.py`                                                                                    |
| Context                     | `case_context_contract.py`, `case_context_deterministic.py`, `context_quality_contract.py`, `evidence_ref.py`                                                                                                                                     |
| Case intelligence (pakiet)  | `case_intelligence/` (`orchestrator`, `desk`, `risks`, `missing_info`, `next_best_action`, `understanding`, `lifecycle`, `validators`, `constants`)                                                                                               |
| Reasoning                   | `business_reasoner.py`, `case_guidance_reasoner.py`, `understanding_output.py`                                                                                                                                                                    |
| Decision                    | `decision_pipeline.py`, `decision_candidate.py`, `decision_projection_blocks.py`                                                                                                                                                                  |
| Policy/action               | `policy_engine.py`, `policy_decision.py`, `policy_action_proposal.py`, `action_proposal_v2.py`, `action_planner.py`                                                                                                                               |
| Write gateway               | `case_write_gateway.py`                                                                                                                                                                                                                           |
| Correlation registry        | `correlation_registry/` (`service`, `store`, `snapshot`, `heuristics`, `auth`, `link_types`, `schema`), `case_engagement_bridge.py`                                                                                                               |
| Projection / feed           | `projection_snapshot_transport.py`, `daszek_v3_operational_feed.py`, `daszek_v3_operational_feed_contract.py`, `daszek_v3_feed_runtime.py`, `dash_projection_v2.py`, `decision_projection_blocks.py`, `daszek_client.py`, `daszek_push_policy.py` |
| Context Projection / Skrzat | `context_tray_set.py`, `projection_envelope.py`, `projection_validator.py`, `skrzat_runtime.py`, `skrzat_copilot.py`, `api_app.py`                                                                                                                |
| Agent runtime               | `agent_runtime/` (`graph`, `run`, `orchestrator`, `materialize`, `planner`, `openai_agent_client`, `circuit_breaker`, `business_pulse`, `authz`, `signal_registry`, `tools/`)                                                                     |
| Shared downstream           | `intake_shared_downstream.py`, `intelligence_shadow_profile.py`, `projection_refresh_contract.py`                                                                                                                                                 |
| Bridge/decision/feedback    | `daszek_bridge_queue_drain.py`, `agent_hitl_bridge.py`, `hitl_gmail_send.py`, `execution_runtime.py`, `operator_feedback_runtime.py`, `feedback_event_contract.py`                                                                                 |
| Signals/replay              | `signal_journal.py`, `signal_worker.py`, `signal_reconciler.py`, `case_state_rebuilder.py`, `signal_contract.py`, `raw_observation_contract.py`                                                                                                   |
| Drive/docs                  | `drive_ingest_runtime.py`, `drive_client.py`, `drive_case_linker.py`, `drive_lane_classifier.py`, `document_intelligence_runtime.py`                                                                                                              |
| Calendar                    | `calendar_runtime.py`, `calendar_client.py`, `calendar_signal_adapter.py`, `calendar_case_linker.py`                                                                                                                                              |
| LLM                         | `groq_client.py`, `llm_provider_router.py`, `embedding_runtime.py`, `agent_runtime/openai_agent_client.py`                                                                                                                                        |
| Eval/quality                | `eval_shadow_analytics.py`, `feedback_analytics_export.py`, `quality_readonly_projection.py`, `quality_readonly_integration.py`, `ai_quality_runtime.py`                                                                                          |
| Fundamenty                  | `exceptions.py` (hierarchia `TopInstalError`), `log_config.py` (JSON logi + ContextVar correlation), `_protocols.py` (DI protocols)                                                                                                               |

## 11. Jak dodawać nową funkcję

### Definition of Done dla małej zmiany

1. **Warstwa** — intake, memory, understanding, decision, policy, action, projection, feedback, signal, registry. Jeśli nie pasuje — nazwij nową warstwę i uzasadnij.
2. **Owner semantyki** — prawda operacyjna, projekcja, czy tekst pomocniczy LLM? Nie przenoś rozstrzygnięć do `../daszek/public/app.js`.
3. **Kontrakt** — pole między warstwami: (a) typ Python / dict schema, (b) normalizacja na granicy, (c) test.
4. **Zapis sprawy** — **zawsze** przez `case_write_gateway`; nie pisz bezpośrednich `UPDATE mailbox_memory_cases`.
5. **Test kontraktowy** — happy path + „złe dane → bezpieczna degradacja”.
6. **Test integracyjny** — dla `gmail_intake` + `decision`: `test_decision_pipeline_intake_integration.py`.
7. **Targeted pytest** — skróć pętlę (sekcja 8).
8. **Proof / runbook** — dla deploymentu/bridge/migracji DB: bounded proof + wpis.
9. **Dokumentacja** — ten README albo wąski doc; „implemented vs aspirational”.

### Styl zmian

- małe helpery zamiast nowej warstwy,
- istniejące kontrakty zamiast równoległych schematów,
- deterministyczny fallback zamiast cichej magii,
- projection-safe payload zamiast raw mail/debug,
- rozdzielenie „proof” / „shadow” / „local test” w PR.

## 12. Typowe sprinty i gdzie pracować

| Sprint                       | Główne pliki                                                                                 | Typowy output                                          |
| ---------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| Case/Task + write path       | `case_write_gateway.py`, `case_routing.py`, `api_app.py` (`/tasks`, `/cases`)                | jeden writer, `requires_action`/`desk_eligible` spójne |
| Correlation / engagement     | `correlation_registry/`, `case_engagement_bridge.py`, `agent_runtime/materialize.py`         | link keyed by engagement, `RegistryLinkConflictError`  |
| Feed v3 (schema 1.3)         | `daszek_v3_operational_feed.py`, `..._contract.py`, `../daszek/includes/api-v3-handlers.php` | `action_items` bez `tasks`, walidacja                  |
| Summary / essence precedence | `decision_projection_blocks.py`, `test_decision_projection_blocks.py`                        | zmiana kolejności pól + test regresji                  |
| ContextQuality               | `context_quality_contract.py`, `case_context_contract.py`                                    | `not_ready_reasons` z allowlistą                       |
| Vector-in-ranking proof      | `mailbox_memory_runtime.py`, `mailbox_memory/`                                               | dowód lokalny rankingu; osobno deployment              |
| Policy/action guardrails     | `policy_decision.py`, `action_proposal_v2.py`                                                | inwarianty v2 + testy                                  |
| Agent runtime / HITL         | `agent_runtime/graph.py`, `materialize.py`, `tools/write_executors.py`                       | plan → HITL → write; zero execution bez approve        |
| Decision execution safety   | `agent_hitl_bridge.py`, `hitl_gmail_send.py`, `execution_runtime.py`, `daszek_bridge_queue_drain.py` | stabilny key, one-effect, recovery i convergence |

## 13. Sekrety i prywatne dane

### Absolutny zakaz (również w promptach zewnętrznych LLM)

- `.env` i pliki z tokenami,
- API keys, cookies, OAuth refresh/access, private keys,
- surowe maile, `body`, `snippet`, załączniki klientów,
- dumpy SQL / pełne backupy,
- wewnętrzne URL-e z osadzonymi tokenami.

### Dobre praktyki w testach i proof

- fixture JSONL z **redakcją** identyfikatorów,
- syntetyczne `message_id` / `case_id`,
- logowanie tylko metadanych (długości, hash'e), nie treści (`log_config.py` — JSON + correlation ContextVar).

## 14. Jak interpretować RFC i dokumenty kierunkowe

Dokument kierunkowy lub historyczny plan może wyjaśniać intencję produktu, ale nie jest dowodem wdrożenia, aktualnego route ani zgody na skutek zewnętrzny.

| Może być | Nie może być |
| --- | --- |
| north-star produktu | proof runtime |
| uzasadnienie „po co” | instrukcja deployu bez runbooka |
| wejście do discovery | pozwolenie na autonomous outbound |
| hipoteza architektoniczna | powód do rewrite stabilnego fundamentu |

Każdy fragment RFC przed implementacją musi zostać potwierdzony w aktualnym kodzie, ograniczony do konkretnego kontraktu i objęty testem. Stan aspiracyjny zapisuj poza dokumentami autorytatywnymi; stan wykonany potwierdza kod i LPS.

## 15. Aktualny praktyczny kierunek

Stan po stabilizacji 2026-07-13:

| Obszar | Status |
| --- | --- |
| signal-active spine | zamrożony baseline |
| Case/Task/Engagement i writer | atomowość potwierdzona na realnym PostgreSQL |
| auth mutacji `/tasks*` | fail-closed, PASS |
| send/reject decision loop | replay-safe i concurrency-safe, PASS |
| UI confirmation | finalny sukces po konwergencji, PASS |
| deployment | lokalny Docker; VPS zawieszony |

Następny kierunek nie polega na budowaniu osobnego workflow dla każdego typu maila. Fundament deterministyczny pozostaje zamrożony, a rozwój powinien poprawiać generalne zdolności intelligence: rozumienie dowolnego sygnału, dobór kompetencji, integrację wyników modułów, aktualizację obrazu sprawy i business-first projection do Daszka.

### Świadomie odkładane

- drugi orchestrator, journal, writer Case lub policy stack;
- duży redesign Daszka przed udowodnieniem potrzeb informacyjnych;
- autonomiczny outbound bez policy/HITL i osobnego proofu;
- automatyczny retry `outcome_unknown`;
- przenoszenie OfferDTO/pricing do Node B;
- architektura retrievalu obok istniejącego Postgres/pgvector bez konkretnej luki.

## 16. Najkrótsza mentalna mapa

```text
Gmail / Drive / Calendar / Cieplo / Daszek
  → RawObservation / CanonicalSignal / journal
  → reconcile active
  → Postgres Case state + evidence                         [Node B = prawda]
  → intelligence: facts / gaps / risks / hypotheses / NBA
  → decision candidate → policy → stable decision_key
  → accepted → execution → durable result
  → completion / feed projection
  → Daszek operator review
  → fresh matching projection → converged confirmation
  → feedback / adjudication → replay / refreshed state
```

LLM interpretuje i planuje. Deterministyczny runtime identyfikuje, autoryzuje, zapisuje, wykonuje, odzyskuje i dowodzi.

## 17. Dokumentacja wtórna

| Dokument | Rola |
| --- | --- |
| `../../../knowledge/INDEX.md` | router workspace |
| `../../../knowledge/source-of-truth.md` | nadrzędne źródła prawdy |
| `../../../knowledge/memory/ACTIVE_WORKSPACE.md` | żywy stan pracy |
| `../../../daszek/docs/core/PROJECT_README.md` | Node A / UI / proxy / bridge |
| `docs/core/CONSTITUTION_V2_1.md` | zasady nadrzędne |
| `docs/core/PHYSICAL_TOPOLOGY.md` | Node A/B i deployment |
| `docs/core/EVENT_CATALOG.md` | katalog eventów i replay semantics |
| `docs/core/AGENT_RUNTIME_ARCHITECTURE.md` | bounded mapa agent runtime |
| `docs/runbooks/LAST_PROVEN_STATE.md` | proof authority |
| `docs/runbooks/MAILBOX_MEMORY_POSTGRES.md` | persistence/concurrency |
| `docs/runbooks/SIGNAL_ACTIVE_ONLY.md` | aktywna ścieżka signal |

Nie odtwarzaj linków do usuniętych `memory-bank`, `archive`, historycznych handoffów ani nieistniejących flow docs.

**Rozstrzyganie konfliktów:** Konstytucja → kod/test → LPS → README.
