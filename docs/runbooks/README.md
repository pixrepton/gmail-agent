# Runbooks

**Status:** aktywny indeks po cleanupie dokumentacji  
**Ostatni przegląd:** 2026-07-13

Runbooki opisują procedury i proof. Nie są katalogiem historycznych epików ani planów.

## Czytaj najpierw

1. [`LAST_PROVEN_STATE.md`](LAST_PROVEN_STATE.md) — najnowszy udowodniony stan i ograniczenia.
2. [`SIGNAL_ACTIVE_ONLY.md`](SIGNAL_ACTIVE_ONLY.md) — jedyna aktywna ścieżka signal runtime.
3. [`MAILBOX_MEMORY_POSTGRES.md`](MAILBOX_MEMORY_POSTGRES.md) — persistence i atomowa mutacja Case.
4. [`SIGNAL_RUNTIME_OPERATOR.md`](SIGNAL_RUNTIME_OPERATOR.md) — operacje signal runtime.
5. [`PACKAGING_AND_SECRETS.md`](PACKAGING_AND_SECRETS.md) — pakowanie i sekrety.

## Aktywne runbooki

| Plik | Rola |
| --- | --- |
| [`LAST_PROVEN_STATE.md`](LAST_PROVEN_STATE.md) | proof authority, freeze i aktualne limity |
| [`SIGNAL_ACTIVE_ONLY.md`](SIGNAL_ACTIVE_ONLY.md) | active-only spine, bridge i lokalny rebuild |
| [`SIGNAL_RUNTIME_OPERATOR.md`](SIGNAL_RUNTIME_OPERATOR.md) | uruchamianie i diagnostyka workera |
| [`MAILBOX_MEMORY_POSTGRES.md`](MAILBOX_MEMORY_POSTGRES.md) | Postgres SoT, atomowość i concurrency |
| [`GMAIL_AGENT_DAILY_OPS.md`](GMAIL_AGENT_DAILY_OPS.md) | lokalna rutyna operacyjna; VPS zawieszony |
| [`PACKAGING_AND_SECRETS.md`](PACKAGING_AND_SECRETS.md) | bezpieczny packaging |

## Zasady higieny

- Nie odtwarzaj usuniętych archiwów, proof-packów i handoffów w aktywnym indeksie.
- Nie linkuj do nieistniejących `archive/`, `truth_flow.md`, dawnych checklist Gate ani historycznych cutoverów.
- Wyniki testów i claimy runtime umieszczaj wyłącznie w LPS.
- Po zmianie procedury aktualizuj właściwy runbook, nie twórz nowego dokumentu o tej samej roli.

Mapa systemu: [`../core/PROJECT_README.md`](../core/PROJECT_README.md).  
Zasady nadrzędne: [`../core/CONSTITUTION_V2_1.md`](../core/CONSTITUTION_V2_1.md).
