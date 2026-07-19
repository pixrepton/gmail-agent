# Pierwszeństwo architektury

**Status:** autorytatywny  
**Zakres:** kolejność źródeł architektonicznych  
**Ostatni przegląd:** 2026-07-13

## Kolejność autorytetu

1. `docs/core/CONSTITUTION_V2_1.md`.
2. Formalne kontrakty V2.1, w tym `docs/core/ARCHITECTURE_AUTHORITY_V2_1.json`.
3. `docs/core/PHYSICAL_TOPOLOGY.md` dla granicy Node A / Node B i modelu deploymentu.
4. Root `AGENTS.md`, repo `AGENTS.md` oraz decyzje operatora w `knowledge/memory/OPERATOR_DECISIONS.md`.
5. Aktualny kod i kontrakty danych, o ile nie naruszają V2.1.
6. Testy kontraktowe, integracyjne i runtime proof.
7. `docs/runbooks/LAST_PROVEN_STATE.md` dla twierdzeń „działa / zostało udowodnione”.
8. Aktywne README i runbooki zgodne z powyższymi źródłami.
9. Dokumenty historyczne wyłącznie jako kontekst.

`LAST_PROVEN_STATE.md` wygrywa nad opisem stanu runtime w README, lecz nie może nadpisać konstytucyjnych granic bezpieczeństwa.

## Rozstrzyganie konfliktu

- **Semantyka i granice:** wygrywa Konstytucja V2.1.
- **Faktyczna implementacja:** wygrywa aktualny kod wraz z testem.
- **Stan uruchomionego środowiska:** wygrywa świeży proof wskazany w LPS.
- **Stan produktu w UI:** musi być zgodny ze stanem Node B i kontraktem konwergencji.
- **Liczby testów:** nie są utrzymywane w dokumentach autorytatywnych; źródłem jest LPS.

## Dokumenty historyczne

Dokument oznaczony `historical`, `superseded`, `archived`, `legacy` albo odnoszący się do usuniętego runtime:

- może wyjaśniać genezę rozwiązania;
- nie może sterować nową implementacją;
- nie może być użyty jako aktualny proof;
- nie powinien znajdować się w aktywnym indeksie dokumentacji.

## Zasada dla agentów

Agent ma:

- zaczynać od Konstytucji, aktualnej pamięci i właściwego README;
- potwierdzać twierdzenia w kodzie lub aktualnym proofie;
- zgłaszać drift dokumentacji;
- nie przywracać historycznych writerów, trybów runtime ani ścieżek omijających auth, idempotencję lub konwergencję.
