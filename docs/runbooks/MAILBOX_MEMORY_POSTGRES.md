# Mailbox Memory Postgres

**Status:** aktywny runbook kontraktu persistence  
**Ostatni przegląd:** 2026-07-13

## Cel

Postgres `mailbox_memory` jest operacyjnym źródłem prawdy Node B. Ten runbook opisuje najważniejszą gwarancję zapisu istniejącego Case oraz minimalne kontrole operacyjne.

## Kanoniczne ścieżki zapisu Case

| Operacja | Kanoniczna ścieżka |
| --- | --- |
| pełne utworzenie / kontrolowany full write | `case_write_gateway.write_case_row(...)` |
| częściowa mutacja istniejącego Case | `case_write_gateway.patch_case_row(...)` → `PostgresMailboxMemoryStore.mutate_case(...)` |
| aktualizacja runtime przez reconciler | `_stamp_case_runtime_state(...)` → `mutate_case(...)` |

Równoległy writer nie może wykonywać sekwencji „fetch poza lockiem → modyfikacja stale obrazu → pełny upsert”. Taki wzorzec powodował last-writer-wins i został zamknięty w concurrency closeout.

## Kontrakt `mutate_case`

Dla tożsamości Case `(scope, owner_id)`:

1. wylicz stabilny advisory lock key:
   - `f"{scope}:{owner_id}".encode("utf-8")`;
   - `hashlib.blake2b(..., digest_size=8)`;
   - `int.from_bytes(..., byteorder="big", signed=True)`;
2. rozpocznij transakcję na jednym połączeniu;
3. `pg_advisory_xact_lock(bigint)`;
4. `SELECT ... FOR UPDATE` aktualnego wiersza;
5. wywołaj szybki, bezskutkowy mutator na aktualnym obrazie;
6. zapisz przez `_upsert_case_payload(..., cur=cur)` tym samym kursorem;
7. `commit`;
8. przy wyjątku: `rollback`; lock zwalnia się wraz z transakcją.

### Niezmienniki

- klucz locka jest deterministyczny między procesami, hostem, kontenerem i restartem;
- Pythonowe `hash()` jest zabronione;
- lock, read, mutate i write muszą używać tego samego połączenia/transakcji;
- mutator nie może zmienić `case_id` ani wykonywać wolnych efektów zewnętrznych;
- drugi writer tego samego Case czeka, a następnie czyta stan po commicie pierwszego;
- rollback nie pozostawia częściowego zapisu;
- ścieżki omijające `mutate_case` wymagają osobnego dowodu, że nie uczestniczą w równoległym read–modify–write.

## Udowodnione zachowanie

Artefakty: `C:\ai-os-case-concurrency-20260713T084431Z`.

Realny test PostgreSQL użył dwóch niezależnych instancji store i dwóch połączeń:

- A przejęło lock, odczytało Case i wstrzymało się;
- B rozpoczęło mutację tego samego Case i oczekiwało;
- A zapisało `latest_signal_id` i wykonało commit;
- B odczytało już nowy stan, dopisało niezależną flagę metadata i wykonało commit;
- końcowy rekord zawierał obie zmiany;
- brak deadlocku;
- osobny proof potwierdził rollback i release locka po wyjątku.

Klasyfikacja: `POSTGRES_ATOMIC_MUTATION_CONFIRMED`.

## Testy kotwiczne

- `tools/gmail_audit/tests/test_case_write_gateway.py`
- `tools/gmail_audit/tests/test_mailbox_memory_store.py`
- `tools/gmail_audit/tests/test_mailbox_memory_runtime.py -k mutate_case`
- `tools/gmail_audit/tests/test_api_tasks.py`
- `tools/gmail_audit/tests/test_signal_reconciler_runtime.py`

## Minimalne kontrole operacyjne

1. Postgres `running` i `healthy`.
2. Właściwy DSN bez ujawniania wartości.
3. Schema i kluczowe tabele obecne.
4. Test realnego store używa testowego/technicznego `case_id` i sprząta dane.
5. Po zmianie `postgres.py` lub wspólnych writerów:
   - targeted tests;
   - pełny suite;
   - rebuild/recreate API i workera, jeśli kod jest bake’owany;
   - SHA-256 host/container;
   - health i restart count;
   - workspace gate.

## Czego nie uznawać za dowód

- samego występowania `updated_at`, `version` lub `ON CONFLICT`;
- testu tylko na `InMemoryMailboxMemoryStore`;
- jednego połączenia bez kontrolowanego przeplotu;
- przejścia pełnego pytest bez realnej semantyki locka;
- `docker cp` bez trwałego rebuild/recreate.
