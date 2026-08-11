# Environment files — Node B (`load_settings`)

**Status:** normative for **local dev** and agent preflight. VPS Compose secrets stay in `.env.vps` per `docs/runbooks/GMAIL_AGENT_DAILY_OPS.md` and `docs/runbooks/PACKAGING_AND_SECRETS.md`.

**Code authority:** `tools/gmail_audit/config.py` (`default_env_candidates`, `load_settings`, `GMAIL_AGENT_ENV_FILE`).

---

## Resolution order (first file wins)

`load_dotenv(..., override=False)` — pierwszy istniejący plik ładuje klucze; późniejsze pliki **nie nadpisują** już ustawionych zmiennych.

| Priority | Path                                       | Typical use                                                       |
| -------- | ------------------------------------------ | ----------------------------------------------------------------- |
| 1        | `GMAIL_AGENT_ENV_FILE` (env var → ścieżka) | Jawny override; **kanoniczna ścieżka AI-OS host-side** to `<repo>/.env.local-vps` |
| 2        | `tools/gmail_audit/.env`                   | Legacy/local compatibility for non-canonical host runs; nie utrzymuj tu aktywnych sekretów AI |
| 3        | **`<repo-root>/.env`**                     | Domyślne dev — Gmail, Groq, Daszek, `MAILBOX_MEMORY_DATABASE_URL` |
| —        | `tools/gmail_audit/.env.local`             | **Nigdy nie ładowany** — scal do `.env` i usuń                    |

Implementacja (2026-05): repo root jest w `default_env_candidates()` obok `CONFIG_DIR/.env`.

---

## Pliki w repozytorium (bez wartości)

| File                          | Loaded by `load_settings`?   | Role                                                                                                                                     |
| ----------------------------- | ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `.env`                        | **Tak** (repo root)          | Główny dev: LLM, Google OAuth, Daszek, Postgres URL, flagi runtime                                                                       |
| `.env.example`                | Nie                          | Szablon — nie commituj sekretów                                                                                                          |
| `.env.vps`                    | Nie (domyślnie)              | **Docker Compose** — hasła Postgres/Neo4j, porty, `GMAIL_AGENT_WORKER_COMMAND`; użyj `docker compose --env-file .env.vps`                |
| `.env.vps.example`            | Nie                          | Szablon VPS compose                                                                                                                      |
| `.env.local-vps`              | **Tak** (mount w kontenerze) | Montowany jako `/etc/topinstal/gmail-agent.env` przez `docker-compose.local-vps.yml` — **gitignored**; szablon: `.env.local-vps.example` |
| `.env.local-vps.example`      | Nie                          | Lokalny dev w Docker: `NEO4J_PILOT_ENABLED=0`, `GMAIL_AGENT_RUNTIME_PROFILE=default`, DB host `mailbox-memory-db`                        |
| `.env.mailbox-memory`         | Nie (domyślnie)              | Profil DB/docker dla mailbox stack; merge kluczy do `.env` lub osobny URL w głównym `.env`                                               |
| `.env.mailbox-memory.example` | Nie                          | Szablon                                                                                                                                  |

**Kanoniczny lokalny SUT / AI-OS:** `.env.local-vps` jest jedynym lokalnym źródłem aktywnych sekretów AI; host-side qualification/judge/Fresh38 powinny ustawiać `GMAIL_AGENT_ENV_FILE=<repo>/.env.local-vps`.

**`tools/gmail_audit/.env`:** zachowaj wyłącznie lokalne ustawienia kompatybilności, które nie konkurują z kanonicznym źródłem sekretów AI.

---

## Lokalna weryfikacja (repo root)

```powershell
cd <repo-root>
python -m compileall tools/gmail_audit scripts -q
python -m pytest tools/gmail_audit/tests -q
python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --verbose
```

**Oczekiwane przy działającym `.env` lub `.env.local-vps` w kontenerze lokalnym:**

- `checks.config.status` = `ok`, `env_source` wskazuje załadowany plik
- `neo4j_pilot`: **skipped** gdy `NEO4J_PILOT_ENABLED=0` (lokalny default w `.env.local-vps.example`) — doctor exit **0** z `--skip-gmail`
- `checks.pgvector` / `mailbox_memory_database` mogą być `failed` gdy stack Postgres nie działa lub API/worker nie są w sieci `gmail-agent-mailbox-memory_default`

**Uwaga:** `GMAIL_AGENT_RUNTIME_PROFILE=canonical_production` wymaga m.in. `NEO4J_PILOT_ENABLED=1` — nie używaj tego profilu w lokalnym `.env.local-vps` bez pełnego kontraktu VPS.

**Pełny doctor** (Gmail + Daszek live): bez `--skip-gmail`; wymaga sieci i tokenów.

**Shadow intelligence (PR-5, przy `SIGNAL_RUNTIME_MODE=active`):**

```powershell
$env:INTELLIGENCE_SHADOW_PROJECTION = "1"
$env:SIGNAL_RUNTIME_MODE = "active"
python -m pytest tools/gmail_audit/tests/test_truth_flow_pr4_pr8.py -q
```

(`legacy` / `shadow` jako `SIGNAL_RUNTIME_MODE` → **ConfigError** — nie używaj.)

---

## Jawny override

```powershell
$env:GMAIL_AGENT_ENV_FILE = "C:\path\to\gmail-agent\.env.vps"   # tylko gdy świadomie testujesz VPS env
python tools/gmail_audit/gmail_intake.py doctor --skip-gmail
```

---

## Truth-flow / PR moduły (kod, nie env)

Po PR-0–PR-8 kluczowe moduły runtime (nie wymagają osobnego `.env`):

| Moduł                | Plik                                                 |
| -------------------- | ---------------------------------------------------- |
| Shared downstream    | `tools/gmail_audit/intake_shared_downstream.py`      |
| Policy attach        | `policy_action_proposal.attach_policy_and_proposals` |
| Shadow profile       | `intelligence_shadow_profile.py`                     |
| Adjudication refresh | `projection_refresh_contract.py`                     |
| Projection transport | `projection_snapshot_transport.py`                   |

Mapa flow: `docs/core/PROJECT_README.md` (oś danych) · `docs/core/AGENT_RUNTIME_ARCHITECTURE.md`.

---

## Fala B / C — env (2026-05-30)

| Zmienna                      | Repo         | Domyślnie       | Wartości                 | Rollback        |
| ---------------------------- | ------------ | --------------- | ------------------------ | --------------- |
| `SIGNAL_EXTRACTION_MODE`     | gmail-agent  | `llm`           | `llm` \| `regex`         | `regex`         |
| `SKRZAT_ANSWER_MODE`         | gmail-agent  | `deterministic` | `deterministic` \| `llm` | `deterministic` |
| `EMAIL_PERSONALIZATION_MODE` | orchestrator | `template`      | `template` \| `llm`      | `template`      |

Orchestrator LLM mail wymaga `NODE_B_REGISTRY_BASE_URL` + `NODE_B_REGISTRY_TOKEN` (ten sam token co Node B API).

Smoke: `docs/runbooks/LAST_PROVEN_STATE.md` § Fala B/C (2026-05-30).

---

## Host vs Docker (agent ingest)

| Kontekst        | `MAILBOX_MEMORY_DATABASE_URL` | Env agent (`AGENT_*`)                                     |
| --------------- | ----------------------------- | --------------------------------------------------------- |
| Host dev (AI-OS canonical) | `127.0.0.1:54129` | `.env.local-vps` przez jawny `GMAIL_AGENT_ENV_FILE`       |
| Host dev (legacy direct run) | `127.0.0.1:54129` | `tools/gmail_audit/.env` / `<repo-root>/.env` według legacy kolejności |
| Kontener worker | `mailbox-memory-db:5432`      | `.env.local-vps` → mount `/etc/topinstal/gmail-agent.env` |

**Zasady:**

- Kontener ustawia `GMAIL_AGENT_ENV_FILE=/etc/topinstal/gmail-agent.env` — **wygrywa** nad mounted `tools/gmail_audit/.env` dla zmiennych agent.
- Nie duplikuj aktywnych sekretów AI między `tools/gmail_audit/.env` i `.env.local-vps`; kanoniczne zmiany wykonuj w `.env.local-vps`.
- `.env.local-vps` tylko **UTF-8** (bez BOM). PowerShell `Add-Content` może zepsuć encoding → worker crash loop.
- Worker **nie bind-mountuje** kodu Python — po zmianach w repo: `docker compose build gmail-agent-worker` + `--force-recreate`.

Runbook ingest: [`../runbooks/SIGNAL_ACTIVE_ONLY.md`](../runbooks/SIGNAL_ACTIVE_ONLY.md).

---

## Agenci — czego nie robić

- Nie commituj `.env`, `.env.vps`, tokenów.
- Nie zakładaj, że `doctor` failed_config = brak PR — sprawdź `env_source` i czy plik jest w kolejności powyżej.
- Nie claimuj Gate B / VPS green tylko dlatego, że pytest przeszedł lokalnie.
- Nie używaj `docker cp` jako trwałego deployu kodu — rebuild obrazu.
