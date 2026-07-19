# TOP-INSTAL AI-OS — Konstytucja V2.1

**Status:** autorytatywny  
**Zakres:** runtime, pamięć, decyzje, wykonanie, projekcja i feedback  
**Ostatni przegląd:** 2026-07-13

## 1. Cel i pierwszeństwo

Ten dokument jest najwyższym źródłem zasad architektonicznych AI-OS TOP-INSTAL. Przy konflikcie dokumentów stosuj kolejność z `ARCHITECTURE_PRECEDENCE.md`. Kod i testy mogą doprecyzować implementację, ale nie mogą cicho naruszać poniższych niezmienników.

## 2. Zasada rdzenia

```text
trwały sygnał → stan → rozumowanie → decyzja → polityka → wykonanie → trwały wynik → projekcja → potwierdzenie
```

- Prawda operacyjna żyje w journalu i trwałym stanie Node B, nie w promptach ani w UI.
- LLM interpretuje, syntetyzuje i planuje; nie jest właścicielem stanu ani dowodem wykonania.
- Deterministyczny runtime odpowiada za identyfikację, autoryzację, idempotencję, retry, wykonanie, audyt i konwergencję.

## 3. Niezmienniki

1. Najpierw append-only journal.
2. `RawObservation` poprzedza `CanonicalSignal`.
3. Ciężkie rozumowanie działa na sygnale kanonicznym, nie bezpośrednio na surowym wejściu.
4. Stan musi być odtwarzalny z journalu, adjudication i deterministycznych reguł.
5. Hot State jest domyślny; Cold Evidence jest pobierane na żądanie.
6. Fakty mają evidence, provenance i confidence; konflikty pozostają jawne.
7. Postgres Node B jest operacyjnym SoT spraw; retrieval i graf są warstwami pomocniczymi.
8. Równoległa mutacja istniejącego Case używa atomowego kontraktu read–modify–write; stale full-row overwrite jest zabroniony.
9. Każda mutacja zewnętrzna działa w modelu default-deny. Brak konfiguracji auth nie może otwierać write route.
10. Każda decyzja operatora ma stabilną tożsamość (`decision_key` lub równoważny kanoniczny klucz).
11. `accepted` nie oznacza `executed`. Przyjęcie, wykonanie, completion/projection i konwergencja są osobnymi etapami.
12. Potwierdzony skutek zewnętrzny nie może zostać automatycznie wykonany drugi raz podczas retry lub replayu.
13. `outcome_unknown` po rozpoczęciu skutku blokuje automatyczny retry i wymaga jawnego recovery.
14. Retry skutku jest dozwolony tylko po jednoznacznym `failed_before_execution`.
15. Finalny event decyzji i trwały wynik wykonania są idempotentne względem stabilnej tożsamości decyzji.
16. Daszek jest projection-only i nie jest semantic ownerem sprawy ani wykonania.
17. UI może pokazać finalny sukces dopiero po odczycie świeżej projekcji Node B zgodnej z właściwym `decision_key` i stanem końcowym.
18. Feedback kalibracyjny i adjudication prawdy pozostają rozdzielone.
19. Każda nowa gwarancja runtime wymaga testu lub równoważnego dowodu oraz wpisu w `LAST_PROVEN_STATE.md`.

## 4. Topologia fizyczna

- **Node A:** WordPress + Daszek — interfejs operatora, transport bridge i lokalny store projekcji.
- **Node B:** `gmail-agent` + Postgres/pgvector + worker — SoT spraw, policy, execution, replay i źródło projekcji.
- Node A i Node B komunikują się przez jawne kontrakty HTTP/bridge; wspólny filesystem nie jest założeniem architektury.
- Kanoniczny proof jest obecnie lokalny w Docker Compose. Produkcyjny VPS pozostaje zawieszony, dopóki operator jawnie nie wznowi deployu.

## 5. Cykl decyzji operatora

Dozwolony model stanów:

```text
received → accepted → executing → executed | outcome_unknown | failed_before_execution → converged
received → accepted → rejected → converged
```

Zasady:

- `accepted` potwierdza przyjęcie komendy, nie skutek biznesowy.
- wynik wykonania jest zapisywany trwale przed completion i projekcją;
- replay stanu `executed` lub `rejected` ponawia najwyżej completion/projection;
- replay `outcome_unknown` nie uruchamia executora;
- konflikt sprzecznych finalnych decyzji musi być jawny i nie może cicho nadpisać wyniku;
- konwergencja oznacza, że aktualna projekcja Node B potwierdza ten sam klucz decyzji i stan finalny.

## 6. Warstwy systemu

1. Intake, journaling, idempotency i replay.
2. Triage, identity i linking.
3. Case Memory / Hot State / facts.
4. Reasoning i conflict model.
5. Decision candidate i policy gate.
6. Execution runtime z trwałym wynikiem.
7. Context/operational projection.
8. Daszek: operator review, decyzja i feedback.
9. Reconcile, recovery i ponowna projekcja.

## 7. Wzorce zabronione

- raw event → heavy reasoning bez normalizacji;
- pełna historia w promptach domyślnie;
- polityka lub auth wyłącznie w UI;
- brak tokenu oznaczający otwarty write route;
- Daszek jako semantic owner lub źródło prawdy wykonania;
- ciche spłaszczanie konfliktów;
- akcja bez `PolicyDecision` i stabilnej tożsamości decyzji;
- automatyczny retry po nieznanym wyniku skutku;
- finalny toast na podstawie samego HTTP 200/accepted;
- drugi journal, drugi writer Case albo równoległy system decyzji bez jawnego RFC.

## 8. Minimalne kontrakty

`RawObservation`, `CanonicalSignal`, `CaseSnapshotHotState`, `EvidenceRef`, `ContextQuality`, `DecisionCandidate`, `PolicyDecision`, `ActionProposal`, `ExecutionResult`, `FeedbackEvent`, `AdjudicationEvent` oraz stabilny kontrakt tożsamości decyzji.

## 9. Reguła implementacji

Nowa praca musi zachować V2.1, nawet gdy starszy README lub historyczny proof opisuje wcześniejszy model. Twierdzenia o działającym runtime pochodzą wyłącznie z aktualnego kodu, świeżych testów i `docs/runbooks/LAST_PROVEN_STATE.md`.
