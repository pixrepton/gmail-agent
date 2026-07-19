# Audyt dokumentacji AI-OS TOP-INSTAL po stabilizacji

Data audytu: 2026-07-13
Zakres: 29 plików z `dokumenty.zip` (`gmail-agent` i `daszek`).

> Uwaga: archiwum nie zawiera root `knowledge/` ani root `AGENTS.md`, więc ich treść nie została bezpośrednio zweryfikowana. Odwołania do `knowledge/memory/ACTIVE_WORKSPACE.md` mogą być poprawne w live workspace, mimo że pliku nie ma w ZIP.

## Werdykt

Dokumentacja wymaga jednej kontrolowanej aktualizacji po stabilizacji. Nie potrzeba kolejnego cleanupu ani przepisywania wszystkiego.

Najważniejsze braki:

1. dwa główne `PROJECT_README.md` opisują stan z 2026-07-10;
2. `LAST_PROVEN_STATE.md` nie zawiera closeoutu concurrency ani zamknięcia AUTH-01 / IDEMP-01 / IDEMP-02 / DEC-01;
3. kontrakt atomowej mutacji Case nie jest opisany w runbooku Postgresa;
4. auth `/tasks*`, trwała idempotencja decyzji, `outcome_unknown` i konwergencja UI nie są zapisane w dokumentach kanonicznych;
5. kilka indeksów i README nadal wskazuje dokumenty usunięte podczas cleanupu;
6. stare wyniki testów (`658`, `1256`, `1331`) są nadal cytowane jako bieżące;
7. lokalne instrukcje rebuild nadal miejscami mówią wyłącznie o workerze, choć wspólny kod Python jest bake’owany również w Node B API.

## Kanoniczny stan, który dokumentacja ma odzwierciedlać

- stabilization baseline: PASS;
- FullStack preflight: 7/7 OK;
- workspace gate: exit 0;
- Case concurrency: `POSTGRES_ATOMIC_MUTATION_CONFIRMED`;
- `patch_case_row` i `_stamp_case_runtime_state` używają atomowego `PostgresMailboxMemoryStore.mutate_case`;
- stabilny advisory lock: BLAKE2b 8 B → signed bigint;
- lock, read, mutate, upsert i commit na jednym połączeniu i w jednej transakcji;
- realny proof PostgreSQL na dwóch niezależnych połączeniach;
- `/tasks*` write routes: fail-closed auth;
- stabilny `decision_key`;
- cykl decyzji: `received → accepted → executing → executed | outcome_unknown | failed_before_execution → converged`;
- reject: `received → accepted → rejected → converged`;
- retry skutku dozwolony tylko po jednoznacznym `failed_before_execution`;
- `outcome_unknown` nie może być automatycznie ponawiany;
- send i reject są replay-safe i concurrency-safe;
- UI nie może traktować HTTP 200/accepted jako finalnego wykonania;
- finalny sukces dopiero po świeżej projekcji potwierdzającej właściwy `decision_key` i stan końcowy;
- pełny gmail-agent: `1350 passed, 5 skipped, 4 subtests passed`;
- Daszek: pytest `8 passed`, Node `13 passed`, JS/PHP syntax PASS;
- API i worker przebudowane, host/container parity zgodne, runtime zdrowy;
- brak deployu na VPS / produkcję.

## Pliki wymagające aktualizacji obowiązkowej

### 1. `gmail-agent/docs/runbooks/LAST_PROVEN_STATE.md`

Dodać:

- concurrency closeout: `C:\ai-os-case-concurrency-20260713T084431Z`;
- audyt sześciu gwarancji: `C:\ai-os-critical-case-stability-audit-20260713T114648Z`;
- closeout czterech findingów: `C:\ai-os-critical-findings-fix-20260713T122855Z`;
- nowy test baseline `1350 passed, 5 skipped, 4 subtests`;
- workspace gate po recreate: exit 0;
- runtime parity i health;
- statusy zamknięte: AUTH-01, IDEMP-01, IDEMP-02, DEC-01;
- aktualny verdict autonomii: `YES, WITH EXPLICIT LIMITS` dla udowodnionego obiegu lokalnego.

Usunąć lub oznaczyć jako historyczne bieżące zdanie o `1331 passed, 3 skipped`.

### 2. `gmail-agent/docs/core/PROJECT_README.md`

To najbardziej nieaktualny dokument centralny.

Poprawić co najmniej:

- wersję i sekcję „Stan na dziś” z 2026-07-10;
- `1256 passed` → nie cytować jako current albo zaktualizować na `1350/5/4`;
- opis `patch_case_row` jako `fetch → merge → write_case_row` — obecnie mutacja jest atomowa przez `mutate_case`;
- sekcję auth API, która mówi, że większość operatorska jest open przy braku env; `/tasks*` write routes są teraz fail-closed;
- dodać lifecycle decyzji, stabilny `decision_key`, `outcome_unknown`, replay-safe send/reject oraz rozdzielenie execution od completion/projection;
- dodać warunek konwergencji UI;
- usunąć/naprawić linki do skasowanych dokumentów;
- uzupełnić mapę plików o `agent_hitl_bridge.py`, `hitl_gmail_send.py`, `execution_runtime.py`, `daszek_bridge_queue_drain.py` jako właścicieli bezpiecznego obiegu decyzji.

### 3. `daszek/docs/core/PROJECT_README.md`

Dodać aktualny kontrakt UI i bridge:

- `accepted` nie oznacza `executed`;
- odpowiedź HTTP 200 może oznaczać tylko przyjęcie decyzji;
- stabilny `decision_key` przekazywany przez PHP proxy;
- finalny toast dopiero po matching decision key w świeżym feedzie;
- stany: accepted / executing / executed / rejected / outcome_unknown / awaiting convergence;
- blokada duplikowanego kliknięcia;
- zachowanie przy timeoutie, starym snapshotcie i feed push failure;
- zaktualizowane testy: Node 13, pytest Daszka 8, syntax checks;
- obecny lokalny PASS zamiast starego „Gate B częściowy/yellow”.

Naprawić linki do usuniętych `WORKSPACE_ONBOARDING`, `truth_flow`, `CONTEXT_PROJECTION_KNOWLEDGE_GRAPH`, jeśli rzeczywiście nie istnieją w live workspace.

### 4. `gmail-agent/docs/runbooks/MAILBOX_MEMORY_POSTGRES.md`

Obecnie jest zbyt ogólny i nie dokumentuje najważniejszej nowej gwarancji.

Dodać:

- `mutate_case` jako kanoniczny atomowy read-modify-write dla istniejącego Case;
- stabilny advisory lock key;
- jedna transakcja i jedno połączenie;
- `SELECT ... FOR UPDATE`;
- rollback/release-lock semantics;
- zachowanie dla brakującego Case;
- mutator nie może zmienić `case_id`;
- dowody i testy z concurrency closeoutu;
- zakaz powrotu do stale full-row read-modify-upsert dla równoległych writerów.

### 5. `gmail-agent/docs/core/CONSTITUTION_V2_1.md`

Jako dokument autorytatywny powinien utrwalić nowe niezmienniki:

- mutacje zewnętrzne default-deny;
- stabilna tożsamość decyzji;
- oddzielenie accepted, execution, completion i convergence;
- potwierdzony skutek nie może być ponawiany;
- `outcome_unknown` wymaga jawnego recovery, bez automatycznego retry;
- Daszek może pokazać finalny sukces dopiero po konwergencji z Node B;
- wykonanie i jego dowód są własnością Node B.

Poprawić topologię „Node B = VPS” na neutralną: Node B jest usługą, obecny kanoniczny proof jest lokalny Docker; VPS pozostaje zawieszony.

### 6. `gmail-agent/docs/core/ARCHITECTURE_AUTHORITY_V2_1.json`

Obecny plik zawiera nieaktualny snapshot `658 passed, 2 skipped` oraz odwołania do dokumentów usuniętych podczas cleanupu.

Zalecane:

- zaktualizować `reviewed_on`;
- usunąć zmienne wyniki pytest z dokumentu autorytatywnego;
- pozostawić runtime proof wyłącznie w `LAST_PROVEN_STATE.md`;
- usunąć nieistniejące `truth_flow`, `CONTEXT_PROJECTION...`, `CODEX...HANDOFF`, `RUNTIME_FLOW_FLAGS_MATRIX`, jeżeli zostały skasowane;
- dodać nowe hard invariants dotyczące auth, decision identity, execution checkpoint, outcome unknown i UI convergence.

### 7. `gmail-agent/docs/core/EVENT_CATALOG.md`

Zweryfikować względem obecnego kodu i opisać:

- stabilne `event_id` dla finalnego reject;
- dokładnie jeden finalny event na decyzję;
- event/result dla HITL send zapisany przed completion/projection;
- statusy execution, w tym `outcome_unknown` i `failed_before_execution`;
- replay nie tworzy kolejnego success/reject eventu;
- zaktualizować datę weryfikacji z 2026-07-10.

Nie dodawać eventów, których kod faktycznie nie emituje — najpierw grep/test.

### 8. `gmail-agent/docs/core/PHYSICAL_TOPOLOGY.md`

Zaktualizować:

- writer Case: `patch_case_row` korzysta z `mutate_case`, a nie zwykłego full upsert;
- kanał Daszek → Node B: auth fail-closed, stabilny decision key;
- bridge drain: trwałe execution result przed completion/projection;
- Node B API i worker są bake’owane w obrazie i oba wymagają rebuild/recreate po zmianie wspólnego kodu Python;
- data przeglądu i aktualne artefakty proof.

### 9. `gmail-agent/docs/runbooks/README.md`

Indeks jest niespójny z cleanupem. Wskazuje wiele plików nieobecnych w pakiecie:

- `AGENT_INGEST_SIGNAL_BATCH.md`;
- `CANONICAL_PRODUCTION_RUNTIME_AND_OPERATIONS.md`;
- `RELEASE_GATE_V2_1.md`;
- `GATE_B_OPERATOR_CHECKLIST_A2.md`;
- `AGENT_SESSION_CONTINUITY.md`;
- `truth_flow.md`;
- całe usunięte `archive/`.

Przepisać indeks do realnego, krótkiego zestawu aktywnych runbooków. Nie odtwarzać usuniętych archiwów.

### 10. `gmail-agent/docs/runbooks/SIGNAL_ACTIVE_ONLY.md`

Poprawić:

- `daszek-bridge-drain (bez zmian)` — bridge został istotnie zmieniony;
- dodać kontrakt decision replay/recovery;
- instrukcja rebuild powinna obejmować API i worker, gdy zmieniono wspólny kod Python;
- usunąć link do nieistniejącego `TARGET_PRODUCT_WORKFLOW.md`, jeżeli został skasowany;
- zaktualizować proof references.

## Pliki wymagające mniejszej korekty lub weryfikacji

### `daszek/docs/ROUTE_MANIFEST.json`

Regenerować z aktualnego kodu po zmianach `api-v3-handlers.php`. Trasy prawdopodobnie nie zmieniły się, ale manifest z 2026-07-07 powinien potwierdzić brak driftu.

### `gmail-agent/AGENTS.md`

Dodać krótki freeze boundary:

- zachować `decision_key` i lifecycle decyzji;
- nie automatyzować retry `outcome_unknown`;
- nie przywracać UI success przed konwergencją;
- `/tasks*` write auth pozostaje fail-closed.

### `gmail-agent/docs/core/AGENT_CONSTITUTION.md`

Doprecyzować, że zatwierdzenie/accepted nie jest dowodem wysłania. Finalny wynik pochodzi z Node B po wykonaniu i konwergencji. Jeśli zmieniony — przeliczyć `AGENT_CONSTITUTION.md.sha256`.

### `gmail-agent/docs/core/AGENT_RUNTIME_ARCHITECTURE.md`

Dokument jest starszy i opisuje część runtime sprzed obecnego modelu. Należy:

- albo zaktualizować go o execution/decision state;
- albo jawnie zawęzić zakres do PR-A–PR-G i wskazać współczesny dokument kanoniczny;
- naprawić link do usuniętego `AGENT_INGEST_SIGNAL_BATCH.md`;
- zweryfikować twierdzenie, że `EngagementSnapshotV2` jest „jedynym SoT operatorskim” względem obecnego Case/Node B SoT.

### `gmail-agent/docs/core/ARCHITECTURE_PRECEDENCE.md`

Naprawić listę dokumentów po cleanupie. Nie może kierować agentów do skasowanych `truth_flow`, `CODEX...HANDOFF`, `CONTEXT_PROJECTION...`.

### `gmail-agent/docs/dev/ENV_LOADING.md`

- naprawić odwołania do usuniętych runbooków;
- dopisać, że wspólny kod Python jest bake’owany również w API, nie tylko workerze;
- opisać istniejące tokeny dla fail-closed mutation auth, jeśli nie wymagały nowej zmiennej env.

### `gmail-agent/docs/runbooks/GMAIL_AGENT_DAILY_OPS.md`

Dokument opisuje aktywne operacje VPS, mimo że produkcja/VPS są zawieszone. Należy wybrać jedną opcję:

1. oznaczyć jako `historical / suspended`, albo
2. przepisać na lokalny daily ops i przenieść VPS do sekcji „po explicit resume”.

Naprawić linki do usuniętego `README-DASZEK.md` i archiwów.

### `gmail-agent/docs/README.md`

Opcjonalnie rozszerzyć aktywny indeks o:

- `core/CONSTITUTION_V2_1.md`;
- `core/PHYSICAL_TOPOLOGY.md`;
- `core/EVENT_CATALOG.md`;
- `runbooks/MAILBOX_MEMORY_POSTGRES.md`.

To nie jest konieczne dla poprawności runtime, ale ułatwi agentom odnalezienie nowych gwarancji.

## Pliki bez wymaganej aktualizacji wynikającej z ostatnich zmian

- `gmail-agent/README.md`;
- `gmail-agent/docs/contracts/engagement_snapshot_v2.schema.json` — pod warunkiem, że generator/model nie zmienił się; nowe statusy decyzji nie muszą należeć do tego schema;
- `gmail-agent/docs/core/CORRELATION_REGISTRY_P0_CONTRACT.md`;
- `gmail-agent/docs/core/LLM_PROVIDER_MAP.md`;
- `gmail-agent/docs/dev/mcp/snippets/agent-runtime.placeholder.jsonc`;
- `gmail-agent/docs/dev/mcp/snippets/custom-ops-mirror.placeholder.jsonc`;
- `gmail-agent/docs/dev/mcp/snippets/serena.placeholder.jsonc`;
- `gmail-agent/docs/runbooks/PACKAGING_AND_SECRETS.md`;
- `gmail-agent/docs/runbooks/SIGNAL_RUNTIME_OPERATOR.md`;
- `gmail-agent/docs/runbooks/templates/release-manifest-export-tree.template.json`.

## Macierz wszystkich 29 plików

| Plik | Decyzja |
|---|---|
| `daszek/docs/ROUTE_MANIFEST.json` | Regenerować / zweryfikować |
| `daszek/docs/core/PROJECT_README.md` | Aktualizacja obowiązkowa |
| `gmail-agent/AGENTS.md` | Mała aktualizacja zalecana |
| `gmail-agent/README.md` | Bez zmian |
| `gmail-agent/docs/README.md` | Mała aktualizacja indeksu zalecana |
| `gmail-agent/docs/contracts/engagement_snapshot_v2.schema.json` | Bez zmian po generator check |
| `gmail-agent/docs/core/AGENT_CONSTITUTION.md` | Małe doprecyzowanie |
| `gmail-agent/docs/core/AGENT_CONSTITUTION.md.sha256` | Przeliczyć tylko po zmianie konstytucji agenta |
| `gmail-agent/docs/core/AGENT_RUNTIME_ARCHITECTURE.md` | Aktualizacja / zawężenie zakresu |
| `gmail-agent/docs/core/ARCHITECTURE_AUTHORITY_V2_1.json` | Aktualizacja obowiązkowa |
| `gmail-agent/docs/core/ARCHITECTURE_PRECEDENCE.md` | Aktualizacja obowiązkowa |
| `gmail-agent/docs/core/CONSTITUTION_V2_1.md` | Aktualizacja obowiązkowa |
| `gmail-agent/docs/core/CORRELATION_REGISTRY_P0_CONTRACT.md` | Bez zmian |
| `gmail-agent/docs/core/EVENT_CATALOG.md` | Aktualizacja obowiązkowa |
| `gmail-agent/docs/core/LLM_PROVIDER_MAP.md` | Bez zmian |
| `gmail-agent/docs/core/PHYSICAL_TOPOLOGY.md` | Aktualizacja obowiązkowa |
| `gmail-agent/docs/core/PROJECT_README.md` | Aktualizacja obowiązkowa |
| `gmail-agent/docs/dev/ENV_LOADING.md` | Mała aktualizacja |
| `gmail-agent/docs/dev/mcp/snippets/agent-runtime.placeholder.jsonc` | Bez zmian |
| `gmail-agent/docs/dev/mcp/snippets/custom-ops-mirror.placeholder.jsonc` | Bez zmian |
| `gmail-agent/docs/dev/mcp/snippets/serena.placeholder.jsonc` | Bez zmian |
| `gmail-agent/docs/runbooks/GMAIL_AGENT_DAILY_OPS.md` | Aktualizacja / oznaczenie suspended |
| `gmail-agent/docs/runbooks/LAST_PROVEN_STATE.md` | Aktualizacja obowiązkowa |
| `gmail-agent/docs/runbooks/MAILBOX_MEMORY_POSTGRES.md` | Aktualizacja obowiązkowa |
| `gmail-agent/docs/runbooks/PACKAGING_AND_SECRETS.md` | Bez zmian |
| `gmail-agent/docs/runbooks/README.md` | Aktualizacja obowiązkowa |
| `gmail-agent/docs/runbooks/SIGNAL_ACTIVE_ONLY.md` | Aktualizacja obowiązkowa |
| `gmail-agent/docs/runbooks/SIGNAL_RUNTIME_OPERATOR.md` | Bez zmian |
| `gmail-agent/docs/runbooks/templates/release-manifest-export-tree.template.json` | Bez zmian |

## Kontrola linków

W dostarczonym archiwum wykryto 35 odwołań do 25 nieobecnych celów w 7 plikach. Część może prowadzić do plików root `knowledge/`, których ZIP nie zawiera. Jednak liczne odwołania do `archive/`, `README-DASZEK.md`, `truth_flow.md`, `AGENT_INGEST_SIGNAL_BATCH.md` i starych release/gate runbooków są zgodne z obrazem dokumentacji sprzed agresywnego cleanupu i wymagają usunięcia lub przekierowania.

## Zalecany sposób aktualizacji

Jedna sesja dokumentacyjna, bez zmian runtime:

1. zweryfikować aktualne symbole i eventy w kodzie przez CBM/grep;
2. zaktualizować 10 dokumentów obowiązkowych;
3. wykonać małe korekty lub weryfikację w 8 dokumentach;
4. nie ruszać 10 plików bez powodu; checksum aktualizować tylko warunkowo;
5. regenerować route manifest i checksum;
6. uruchomić link checker, JSON parse, markdown sanity, checksum i istniejący docs/checklist gate;
7. nie zmieniać kodu, envów, kontenerów ani testów runtime;
8. zapisać jeden artifact report i minimalnie zaktualizować pamięć po PASS.
