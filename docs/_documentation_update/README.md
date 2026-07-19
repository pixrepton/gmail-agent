# AI-OS TOP-INSTAL — pakiet zaktualizowanej dokumentacji

**Data:** 2026-07-13  
**Zakres:** wszystkie 29 plików dostarczonych w `dokumenty.zip`  
**Charakter zmian:** wyłącznie dokumentacja; bez modyfikacji runtime, envów, kontenerów lub danych

## Co zawiera pakiet

- pełną strukturę `gmail-agent/` i `daszek/` gotową do wklejenia do workspace;
- 18 profesjonalnie zaktualizowanych plików;
- 11 plików pozostawionych bez zmian po weryfikacji;
- `CHANGELOG.md` z opisem zmian;
- `VALIDATION_REPORT.md` z wynikami kontroli;
- `documentation.patch` jako techniczny diff;
- `FILE_MANIFEST_SHA256.txt` do weryfikacji integralności.

## Najważniejsze utrwalone gwarancje

1. `POSTGRES_ATOMIC_MUTATION_CONFIRMED` dla równoległych mutacji Case.
2. Fail-closed auth dla aktywnych write routes `/tasks*`.
3. Stabilny `decision_key` i rozdzielenie `accepted`, execution, completion oraz convergence.
4. Trwały execution result przed completion/projection.
5. Replay-safe send i reject; brak automatycznego retry `outcome_unknown`.
6. Finalny sukces Daszka dopiero po świeżej projekcji potwierdzającej właściwy decision key.
7. Aktualny lokalny baseline: gmail-agent `1350 passed, 5 skipped, 4 subtests`, Daszek pytest `8 passed`, Node `13 passed`, workspace gate `exit 0`.

## Instalacja

1. Zachowaj kopię obecnych dokumentów.
2. Skopiuj katalogi `gmail-agent/` i `daszek/` do root workspace, zachowując strukturę ścieżek.
3. Nie kopiuj katalogu `_documentation_update/` do repo, chyba że chcesz zachować raporty.
4. Przejrzyj `documentation.patch` przed commitem.
5. Po wklejeniu uruchom repozytoryjny docs/checklist gate, jeśli istnieje w live workspace.

## Ograniczenie zakresu

Archiwum źródłowe nie zawierało root `knowledge/` ani root `AGENTS.md`; nie zostały więc fizycznie zmodyfikowane. Dokumenty w pakiecie odnoszą się do kanonicznych plików `knowledge/` zgodnie z aktualnym workspace.

`daszek/docs/ROUTE_MANIFEST.json` pozostawiono bez zmian: ostatnie zmiany dotyczyły semantyki odpowiedzi i konwergencji, nie rejestracji route. Źródłowe pliki rejestracji tras nie były częścią dostarczonego pakietu, więc nie sfabrykowano nowej daty generacji.
