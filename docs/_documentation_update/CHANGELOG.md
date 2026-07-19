# Changelog dokumentacji — 2026-07-13

Zmieniono **18 z 29** dostarczonych plików.

| Plik | Zmiana |
| --- | --- |
| `daszek/docs/core/PROJECT_README.md` | naprawiono kodowanie UTF-8; dodano decision lifecycle, auth, idempotencję, konwergencję UI, aktualne testy i proof; uporządkowano numerację oraz martwe linki |
| `gmail-agent/AGENTS.md` | dodano freeze boundaries dla atomowości Case, auth, decision key, outcome_unknown i konwergencji UI |
| `gmail-agent/docs/README.md` | przepisano aktywny indeks dokumentacji po cleanupie |
| `gmail-agent/docs/core/AGENT_CONSTITUTION.md` | doprecyzowano różnicę approval/accepted/executed, outcome_unknown i rolę trwałego ExecutionResult |
| `gmail-agent/docs/core/AGENT_CONSTITUTION.md.sha256` | przeliczono checksum po zmianie konstytucji agenta |
| `gmail-agent/docs/core/AGENT_RUNTIME_ARCHITECTURE.md` | zawężono dokument do PR-A–PR-G, usunięto nadmierny claim SoT, dodano aktualny kontrakt decision execution i oznaczono historyczne sekcje |
| `gmail-agent/docs/core/ARCHITECTURE_AUTHORITY_V2_1.json` | usunięto zmienne wyniki testów i martwe dokumenty; dodano aktualne hard invariants |
| `gmail-agent/docs/core/ARCHITECTURE_PRECEDENCE.md` | przepisano kolejność autorytetu po cleanupie dokumentacji |
| `gmail-agent/docs/core/CONSTITUTION_V2_1.md` | rozszerzono konstytucję o deterministyczny runtime, atomowość, default-deny, decision lifecycle, outcome_unknown i UI convergence |
| `gmail-agent/docs/core/EVENT_CATALOG.md` | dodano semantykę finalnych eventów, stabilnej tożsamości, replayu i execution result |
| `gmail-agent/docs/core/PHYSICAL_TOPOLOGY.md` | zaktualizowano Node A/B, atomowy writer, kanały auth/bridge, packaging, rebuild i parity |
| `gmail-agent/docs/core/PROJECT_README.md` | kompleksowa aktualizacja baseline, write path, API auth, decision runtime, onboarding, walidacji i kierunku intelligence-first |
| `gmail-agent/docs/dev/ENV_LOADING.md` | usunięto nieaktywne linki VPS, dodano tokeny mutation auth oraz rebuild API+worker |
| `gmail-agent/docs/runbooks/GMAIL_AGENT_DAILY_OPS.md` | zastąpiono historyczną rutynę VPS aktywną procedurą local Docker |
| `gmail-agent/docs/runbooks/LAST_PROVEN_STATE.md` | dodano concurrency closeout, audyt, zamknięcie AUTH/IDEMP/DEC, aktualne testy, parity, health i limity autonomii |
| `gmail-agent/docs/runbooks/MAILBOX_MEMORY_POSTGRES.md` | rozbudowano do pełnego kontraktu mutate_case, locka, transakcji, rollbacku i proofu dwóch połączeń |
| `gmail-agent/docs/runbooks/README.md` | usunięto martwe indeksy/archive i pozostawiono realny zestaw aktywnych runbooków |
| `gmail-agent/docs/runbooks/SIGNAL_ACTIVE_ONLY.md` | zaktualizowano active-only flow, bridge decision safety i rebuild obu usług Node B |

## Pliki zweryfikowane bez zmian

- `daszek/docs/ROUTE_MANIFEST.json`
- `gmail-agent/README.md`
- `gmail-agent/docs/contracts/engagement_snapshot_v2.schema.json`
- `gmail-agent/docs/core/CORRELATION_REGISTRY_P0_CONTRACT.md`
- `gmail-agent/docs/core/LLM_PROVIDER_MAP.md`
- `gmail-agent/docs/dev/mcp/snippets/agent-runtime.placeholder.jsonc`
- `gmail-agent/docs/dev/mcp/snippets/custom-ops-mirror.placeholder.jsonc`
- `gmail-agent/docs/dev/mcp/snippets/serena.placeholder.jsonc`
- `gmail-agent/docs/runbooks/PACKAGING_AND_SECRETS.md`
- `gmail-agent/docs/runbooks/SIGNAL_RUNTIME_OPERATOR.md`
- `gmail-agent/docs/runbooks/templates/release-manifest-export-tree.template.json`

## Świadomie niezmieniony manifest tras

`daszek/docs/ROUTE_MANIFEST.json` zachowuje datę 2026-07-07. Zmiany z 2026-07-13 nie dodawały ani nie usuwały tras według dostarczonych raportów; zmieniały payloady, auth i semantykę UI. Bez źródłowych plików rejestracji tras nie generowano fikcyjnego manifestu.
