# gmail-agent — operacje dzienne

**Status:** aktywny runbook lokalny  
**Deployment:** lokalny Docker Compose  
**VPS/produkcja:** zawieszone od 2026-06-17; wymagają jawnej decyzji operatora

## Kiedy używać

| Sytuacja | Procedura |
| --- | --- |
| zmiana wspólnego kodu Python Node B | testy → build/recreate API + worker → parity → health → gate |
| zmiana tylko Daszka PHP/JS | syntax/tests → bind-mount parity → smoke/gate |
| zmiana kontraktu feed/bridge | testy obu repo → lokalny integrated proof → gate |
| rutynowa kontrola | preflight + health + workspace gate |
| incydent | zachowaj logi/artefakt, odtwórz minimalnie, nie wykonuj realnego replayu bez izolacji |

## 1. Preflight

Z root workspace:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\preflight-local-stack.ps1 -FullStack
```

Oczekiwane: `7/7 OK` i `exit 0`.

## 2. Testy przed zmianą runtime

W repo `gmail-agent`:

```powershell
python -m compileall tools/gmail_audit scripts -q
python -m pytest tools/gmail_audit/tests -q --tb=line
```

Dla Daszka:

```powershell
node --check public/app.js
php -l includes/api-v3-handlers.php
python -m pytest tests -q --tb=line
node --test tests/test_row4b_note_hitl_approve.node.js
```

Uruchamiaj targeted tests wcześniej; pełny suite jest warunkiem closeoutu, nie pierwszą pętlą debugowania.

## 3. Rebuild/recreate Node B

Wspólny kod `tools/gmail_audit` jest bake’owany w obrazie używanym przez API i worker. Po jego zmianie przebuduj tylko wymagane usługi, standardowo obie:

```powershell
cd gmail-agent
docker compose --env-file .env.vps -f docker-compose.local-vps.yml build gmail-agent-nodeb-api gmail-agent-worker
docker compose --env-file .env.vps -f docker-compose.local-vps.yml --profile api --profile worker up -d --force-recreate gmail-agent-nodeb-api gmail-agent-worker
```

Nie używaj `docker cp` jako finalnego wdrożenia.

Daszek jest lokalnie bind-mountem; po zmianie pluginu zwykle nie wymaga rebuild, ale wymaga testu i weryfikacji SHA/odczytu z kontenera.

## 4. Health i parity

Sprawdź:

- Node B API `/health`;
- worker `/system/worker/health`;
- Postgres `running/healthy`;
- `restart_count=0` po recreate;
- brak crash loop i nowych błędów DB/execution w logach;
- SHA-256 host/container dla zmienionych plików.

Nie zapisuj sekretów, DSN ani treści wiadomości w artefaktach.

## 5. Workspace gate

Po recreate:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-local-gates.ps1
```

Oczekiwane: `exit 0`. Nie osłabiaj etapów gate i nie pomijaj błędów collection.

## 6. Obieg decyzji operatora

Przy problemach send/reject/bridge sprawdź kolejno:

1. auth i token scope;
2. stabilny `decision_key`;
3. trwały execution result;
4. status `failed_before_execution` vs `outcome_unknown`;
5. completion/bridge acknowledgement;
6. feed refresh;
7. matching decision key w świeżej projekcji;
8. dopiero wtedy finalne potwierdzenie UI.

`outcome_unknown` nie jest automatycznie ponawiany.

## 7. Produkcyjny VPS

Historyczne skrypty i compose VPS nie są aktywną rutyną. Nie wykonuj sync, SSH, timerów ani deployu bez jawnego polecenia operatora i osobnego planu proof/deploy/rollback.

## 8. Proof

Aktualny stan i artefakty: [`LAST_PROVEN_STATE.md`](LAST_PROVEN_STATE.md).  
Persistence/concurrency: [`MAILBOX_MEMORY_POSTGRES.md`](MAILBOX_MEMORY_POSTGRES.md).
