# Validation report — dokumentacja AI-OS TOP-INSTAL

**Data:** 2026-07-13  
**Wynik:** PASS

## Zakres

- pliki źródłowe: **29**;
- zmienione: **18**;
- zweryfikowane bez zmian: **11**;
- zmiany runtime/env/data: **0**.

## Kontrole

| Kontrola | Wynik |
| --- | --- |
| JSON parse | PASS — 5 pliki |
| Markdown parse / fenced blocks | PASS — 23 pliki |
| Względne linki do plików w pakiecie | PASS — brak martwych linków |
| UTF-8 / mojibake | PASS |
| AGENT_CONSTITUTION SHA-256 | PASS |
| Usunięte runbooki/handoffy w aktywnych odwołaniach | PASS |
| ROUTE_MANIFEST integralność | PASS — świadomie bez regeneracji |
| Niezmienione pliki zachowały bytes | PASS — 11 pliki |

## Kluczowe kontrole merytoryczne

- aktualny baseline i proof są skupione w `LAST_PROVEN_STATE.md`;
- zmienne wyniki testów usunięto z dokumentów autorytatywnych;
- `patch_case_row` jest opisany przez atomowy `mutate_case`, nie stale full-row overwrite;
- `/tasks*` write auth jest opisany jako fail-closed;
- `accepted` jest oddzielone od `executed` i `converged`;
- `outcome_unknown` blokuje automatyczny retry;
- send/reject mają stabilną tożsamość i replay-safe finality;
- Daszek potwierdza finalny wynik dopiero po fresh matching projection;
- local Docker jest aktywnym deploymentem, a VPS jest zawieszony;
- dokumentacja kieruje do kanonicznej pamięci `knowledge/`, nie do usuniętego repo-local memory-bank.

## Świadome ograniczenia

- Root `knowledge/` i root `AGENTS.md` nie były częścią archiwum źródłowego, więc nie zostały zmodyfikowane.
- `ROUTE_MANIFEST.json` nie został przegenerowany, ponieważ pakiet nie zawierał wszystkich źródeł rejestracji route. Raportowane zmiany dotyczyły auth, payloadów i semantyki UI, nie dodawania/usuwania tras.
- Proof paths `C:\...` pozostają referencjami do lokalnych artefaktów operatora, nie plikami pakietu.
