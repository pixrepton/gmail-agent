# gmail-agent — wyłącznie signal-active

**Status:** obowiązujący kontrakt runtime  
**Ostatni przegląd:** 2026-07-13

## Jedna ścieżka Gmail

```text
signal-worker | signal-run
→ RawObservation / CanonicalSignal
→ SignalJournal
→ reconcile_signal
→ shared downstream
→ trwały stan Node B
→ operational feed
```

`legacy`, `shadow` oraz `SIGNAL_RUNTIME_COMPAT` nie są wspieranymi alternatywami dla `process_snapshot`. Nie przywracaj ich bez jawnej decyzji architektonicznej.

## Minimalny profil

```env
SIGNAL_RUNTIME_MODE=active
SIGNAL_WORKER_ENABLED=1
GMAIL_INGRESS_OWNER=signal_worker
GMAIL_CHANGE_DETECTION_ENABLED=1
INTAKE_LLM_BEFORE_SIGNAL=0
MAILBOX_MEMORY_DATABASE_URL=postgresql://...
DASZEK_V2_PUSH=0
```

Nie kopiuj wartości sekretów do dokumentacji. `SIGNAL_RUNTIME_MODE` domyślnie pozostaje `active`; wartości historyczne powodują `ConfigError`.

## Komendy

| Cel | Komenda |
| --- | --- |
| worker/poll | `python tools/gmail_audit/gmail_intake.py signal-worker` |
| jednorazowy przebieg | `python tools/gmail_audit/gmail_intake.py signal-run --oneshot --verbose` |
| replay techniczny | `python tools/gmail_audit/gmail_intake.py signal-replay ...` — tylko na izolowanym rekordzie/proofie |
| bridge operatora | `python tools/gmail_audit/gmail_intake.py daszek-bridge-drain ...` |
| doctor | `python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --verbose` |

Legacy commands `message`, `period`, `batch`, `shadow-run` nie są ścieżką live Gmail.

## Bridge i decyzje

`daszek-bridge-drain` jest częścią chronionego obiegu decyzji i nie jest „niezmienionym legacy helperem”. Obowiązuje:

- stabilny `decision_key`/queue identity;
- auth write routes default-deny;
- trwały wynik execution przed completion/projection;
- send/reject replay-safe;
- retry skutku tylko po `failed_before_execution`;
- `outcome_unknown` blokuje automatyczny retry;
- finalny sukces UI dopiero po konwergencji feedu.

## Local Docker

Dwa konteksty env:

| Kontekst | Typowy DSN | Źródło env |
| --- | --- | --- |
| host dev | `127.0.0.1:54129` | `.env` / jawny `GMAIL_AGENT_ENV_FILE` |
| kontener | `mailbox-memory-db:5432` | `.env.local-vps` montowany do `/etc/topinstal/gmail-agent.env` |

Wspólny kod Python jest bake’owany zarówno w `gmail-agent-nodeb-api`, jak i `gmail-agent-worker`. Po jego zmianie:

```powershell
cd gmail-agent
docker compose --env-file .env.vps -f docker-compose.local-vps.yml build gmail-agent-nodeb-api gmail-agent-worker
docker compose --env-file .env.vps -f docker-compose.local-vps.yml --profile api --profile worker up -d --force-recreate gmail-agent-nodeb-api gmail-agent-worker
```

Następnie wykonaj host/container parity, health, restart count i workspace gate.

## Kontrole

```powershell
python -m compileall tools/gmail_audit scripts -q
python -m pytest tools/gmail_audit/tests -q --tb=line
python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --verbose
```

Pełny stan proof: [`LAST_PROVEN_STATE.md`](LAST_PROVEN_STATE.md).

## Zakazy

- brak realnego replayu wiadomości operatora bez izolacji;
- brak automatycznego retry `outcome_unknown`;
- brak `docker cp` jako finalnego deploymentu;
- brak VPS/prod operacji bez explicit operator resume;
- brak drugiego intake/reconcile obok signal-active.
