# Topologia fizyczna TOP-INSTAL

**Status:** aktywny dokument interpretacyjny  
**Zakres:** Node A/Node B, granice filesystemu, kanały komunikacji i lokalny Docker  
**Ostatni przegląd:** 2026-07-13

> Node A canonical: `../daszek/` + `../wp-bridges/`. Node B canonical: repo `gmail-agent`.

## Kluczowa zasada

Najpierw ustal, gdzie fizycznie działa komponent, kto jest właścicielem stanu i przez jaki kontrakt przekracza granicę węzłów. Produkcyjny VPS jest zawieszony; kanoniczny proof wykonywany jest lokalnie w Docker Compose.

## Lokalny stack

| Usługa | Port hosta | Compose / obraz | Rola |
| --- | --- | --- | --- |
| Daszek WordPress | `8090` | root `docker-compose.daszek-local.yml` | Node A — UI, feed store, proxy i bridge queue |
| gmail-agent Node B API | zwykle `8766 → 8765` | `gmail-agent/docker-compose.local-vps.yml` | FastAPI, auth, Case/decision API |
| gmail-agent worker | bez host portu | ten sam obraz runtime | signal-worker, reconcile, feed, bridge drain |
| Mailbox memory Postgres | `54129` | `pgvector/pgvector:pg16` | operacyjny SoT spraw i execution state |
| RAG backend | `8000` | osobny compose | retrieval/KB |
| kalk-top | `8091` | osobny compose | właściciel OfferDTO i HVAC calculations |
| cieplo-orchestrator | zależnie od compose | osobny runtime | workflow Cieplo; korelacja przez jawne kontrakty |

Preflight: `scripts/preflight-local-stack.ps1 -FullStack`.  
Workspace gate: `scripts/verify-local-gates.ps1`.

## Node A — Daszek

- WordPress + operator SPA.
- Przechowuje projekcję feed v3 i bounded overlay/bridge queue.
- Nie jest SoT semantyki Case, decyzji ani wykonania.
- Przyjęcie HTTP 200 nie jest finalnym skutkiem.
- Finalne UI confirmation wymaga świeżej projekcji z matching `decision_key` i statusem końcowym.
- Plugin jest lokalnie bind-mountem; po zmianach wymagane są syntax/tests i parity, zwykle bez rebuild kontenera.

## Node B — gmail-agent

- Python runtime + Postgres/pgvector + worker.
- Odpowiada za intake, journal, Case state, policy, auth, execution, replay/recovery i źródło projekcji.
- Kanoniczne create/full writes przechodzą przez `case_write_gateway.write_case_row`.
- Częściowe mutacje istniejącego Case przechodzą przez `patch_case_row` → `PostgresMailboxMemoryStore.mutate_case`.
- `_stamp_case_runtime_state` również używa atomowej mutacji.
- Decision execution zapisuje trwały wynik przed completion/projection.
- API i worker używają kodu bake’owanego w obrazie; zmiana wspólnego Pythona wymaga rebuild/recreate obu usług, jeśli obie wykonują zmieniony kod.

## Granica filesystemu

- Node B nie zakłada bezpośredniego odczytu storage WordPress.
- Node A nie odczytuje bezpośrednio bazy Node B.
- Brak `wp-content/uploads` w Node B nie jest błędem runtime.
- Lokalny folder repo Daszka jest kodem dev/bind-mountem, nie wspólnym SoT.

## Kanały komunikacji

| Kanał | Kierunek | Kontrakt bezpieczeństwa |
| --- | --- | --- |
| Operational feed push | Node B → Node A | walidowany feed v3; auth bridge/session; best-effort transport nie jest execution proof |
| Node B read API | Daszek proxy → Node B | bearer zgodnie z endpointem; read nie nadaje write scope |
| `/tasks*` i inne mutacje | Daszek/service → Node B | fail-closed write auth; unauthorized kończy się przed store |
| Bridge queue/drain | Node A → Node B | stabilny queue/decision key, replay-safe completion |
| HITL send/reject | Node A → Node B | policy/HITL, trwały execution result, no auto-retry `outcome_unknown` |
| Internal registry | cross-repo → Node B | `POST /internal/registry/links` + registry bearer |
| Materialize | Daszek → Node B | owner/CSRF po stronie WP + bearer + idempotency key Node B |

## Krytyczny obieg decyzji

```text
Daszek click
→ CSRF/owner + Node B bearer
→ received/accepted z decision_key
→ executing
→ durable executed | rejected | outcome_unknown | failed_before_execution
→ completion / bridge acknowledgement
→ feed refresh
→ matching fresh projection
→ converged UI confirmation
```

`accepted` i `converged` są różnymi stanami. Replay finalnego skutku nie uruchamia executora drugi raz; replay może ponowić completion/projection.

## Runtime packaging i parity

Po zmianie wspólnego kodu Node B:

1. targeted tests i pełny suite;
2. build `gmail-agent-nodeb-api` i `gmail-agent-worker`;
3. force recreate tylko wymaganych usług;
4. SHA-256 host/container zmienionych plików;
5. API/worker/Postgres health i restart count;
6. workspace gate po recreate.

`docker cp` nie jest trwałym deploymentem.

## Reguła diagnostyczna

Przy problemie decyzji/bridge/feed sprawdź kolejno:

1. auth, CSRF, URL i token scope;
2. `decision_key` i status execution;
3. trwały execution result;
4. completion/bridge queue;
5. feed push/readback;
6. matching fresh projection w UI;
7. logi i health obu węzłów.

## Dowody

- concurrency: `C:\ai-os-case-concurrency-20260713T084431Z`;
- auth/idempotency/UI convergence: `C:\ai-os-critical-findings-fix-20260713T122855Z`;
- aktualny snapshot: `docs/runbooks/LAST_PROVEN_STATE.md`.

Powiązane: `docs/core/CONSTITUTION_V2_1.md`, `docs/core/PROJECT_README.md`, `../daszek/docs/core/PROJECT_README.md`.
