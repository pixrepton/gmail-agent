# LLM Provider Map

**Weryfikacja:** 2026-07-18 (DEEPSEEK-MIGRATION-1, vs `central_llm_stage.py`, `groq_client.py`,
`agent_runtime/settings.py`, `agent_runtime/openai_agent_client.py`, `config.py`)

Mapa providerów i modeli używanych w pipeline Gmail-Agent. Źródło prawdy:
`tools/gmail_audit/central_llm_stage.py`, `groq_client.py`, `config.py`,
`agent_runtime/settings.py`.

## DeepSeek — priorytet #1 (obie ścieżki)

Od DEEPSEEK-MIGRATION-1, **DeepSeek V4 Flash jest priorytetem #1** w obu niezależnych łańcuchach
poniżej (structured stages i agent planner), gdy `DEEPSEEK_API_KEY` jest ustawiony. Wszystkie
dotychczasowe providery/fallbacki są zachowane bez zmian, przesunięte o jedną pozycję.

- Model: `deepseek-v4-flash` (`DEEPSEEK_MODEL`); endpoint OpenAI-compatible
  `https://api.deepseek.com` (`DEEPSEEK_BASE_URL`).
- Thinking mode **domyślnie ON** (`DEEPSEEK_THINKING_ENABLED=true`), `reasoning_effort=high`
  (`DEEPSEEK_REASONING_EFFORT`) — wysyłane jako `thinking={"type":"enabled"}` +
  `reasoning_effort` w każdym requeście do DeepSeek (structured: top-level JSON body pole w
  `_post_openai_chat_structured`; agent planner: `extra_body` w `client.chat.completions.create`).
- `reasoning_content` (chain-of-thought DeepSeek) nigdy nie jest odczytywany jako business
  output ani argument narzędzia — tylko `message.content` (structured) / `tool_calls` +
  `finish_reason` (planner) są parsowane. Agent planner nie prowadzi multi-round rozmowy w
  ramach jednego wywołania klienta (każda tura `plan_next_tool()` to nowy, bezstanowy request —
  stan biznesowy niesie `EngagementSnapshotV2`, nie surowa historia wiadomości), więc wymóg
  DeepSeek "przekaż `reasoning_content` w kolejnym requeście przy tool call" nie ma zastosowania
  w tym kodzie — nie ma kontynuacji tej samej rozmowy do której trzeba by je doklejać.
- Brak klucza → DeepSeek pomijany, reszta łańcucha bez zmian (bez importowego crasha).
  Błędy operacyjne DeepSeek, w tym rate limit, timeout, transient/5xx oraz auth/config failure
  (`401/403`, zły lub wygasły klucz), przechodzą do poprzedniego priorytetu #1 jako jawna
  degradacja primary providera. Błędy naszego adaptera/kontraktu requestu (`400`,
  `unsupported parameter`, `tool_choice`, malformed payload) są fail-fast i nie mogą być
  cicho maskowane fallbackiem.
- Konfiguracja: `DEEPSEEK_API_KEY`, `DEEPSEEK_API_KEYS` (multi-key pool), `DEEPSEEK_MODEL`,
  `DEEPSEEK_BASE_URL`, `DEEPSEEK_THINKING_ENABLED`, `DEEPSEEK_REASONING_EFFORT`.

## Structured stages (intake, signal, business, guidance, reply)

Wszystkie fazy wołają `run_central_structured_stage` / `run_structured_stage` z
`stage_name` poniżej. Provider wybiera `primary_llm_provider()` + łańcuch z
`LLM_PRIMARY_PROVIDER` / `LLM_FALLBACK_PROVIDERS` / opcjonalnej alternacji.

| stage_name (`stage_name=`) | Moduł wywołujący            | Model (domyślnie)                      | Timeout (kod)        |
| -------------------------- | --------------------------- | -------------------------------------- | -------------------- |
| `intake_reasoning`         | `gmail_intake.py`           | `GROQ_MODEL` → `openai/gpt-oss-120b`   | client 30s, hard 60s |
| `signal_extraction`        | `signal_extractor.py`       | j.w.                                   | j.w.                 |
| `business_reasoning`       | `business_reasoner.py`      | j.w.                                   | j.w.                 |
| `case_guidance`            | `case_guidance_reasoner.py` | `CASE_GUIDANCE_MODEL` lub `GROQ_MODEL` | j.w.                 |
| `reply_drafter`            | `reply_drafter.py`          | j.w.                                   | j.w.                 |

> **Uwaga:** dokumentacja historyczna używała nazwy `reply_draft` — w kodzie stage to
> **`reply_drafter`**. Nie ma osobnego stage `agent_subagent`; sub-agenty to routing
> narzędzi (`agent_runtime/sub_agents.py`), nie osobna faza LLM.

### Provider (structured)

| Priorytet | Warunek                                            | Provider                                                                                             |
| --------- | -------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| **1**     | `DEEPSEEK_API_KEY` ustawiony                       | **DeepSeek** (`deepseek-v4-flash`, `DEEPSEEK_BASE_URL`, thinking mode ON, `reasoning_effort=high`)   |
| 2         | `ANTHROPIC_API_KEY` ustawiony                      | **Anthropic** (`TopInstalLLMClient`, model `ANTHROPIC_MODEL` → domyślnie `claude-sonnet-4-20250514`) |
| 3         | `LLM_PRIMARY_PROVIDER` (domyślnie z `LLM_BACKEND`) | **groq** (domyślnie), **cerebras**, **nvidia**, **openai_chat**                                      |
| 4         | `LLM_FALLBACK_PROVIDERS` (env, domyślnie puste)    | kolejno po przecinku/spacji, np. `cerebras nvidia`                                                   |

DeepSeek (priorytet 1) jest próbą poza tym łańcuchem (tak jak Anthropic). Operacyjna porażka
DeepSeek lub auth/config failure spada do priorytetu 2/3/4 bez zmian ich własnej logiki.
Adapter/request contract bug na DeepSeek jest fail-fast. Sukces DeepSeek zwraca wynik
natychmiast bez próbowania Anthropic/routera.

Gdy `LLM_STRUCTURED_PROVIDER_ALTERNATION=1` (domyślnie **włączone**, jeśli są
`GROQ_API_KEY` + `CEREBRAS_API_KEY` + `CEREBRAS_BASE_URL` i `LLM_BACKEND=groq`):
pierwszy slot na request rotuje **groq ↔ cerebras** per `stage_name`; na błędzie
transient lub provider-local (404/408, 429, 5xx, timeout) nadal działają
`LLM_FALLBACK_PROVIDERS`.

**Nie ma** automatycznego łańcucha Groq → Cerebras → NVIDIA bez konfiguracji —
trzeba ustawić `LLM_FALLBACK_PROVIDERS` (np. `cerebras nvidia`) lub włączyć alternację.

### Modele domyślne (structured)

| Provider  | Env model         | Domyślna wartość                 |
| --------- | ----------------- | -------------------------------- |
| Groq      | `GROQ_MODEL`      | `openai/gpt-oss-120b`            |
| Cerebras  | `CEREBRAS_MODEL`  | `openai/gpt-oss-120b` (jak Groq) |
| NVIDIA    | `NVIDIA_MODEL`    | `meta/llama-3.3-70b-instruct`    |
| Anthropic | `ANTHROPIC_MODEL` | `claude-sonnet-4-20250514`       |
| DeepSeek  | `DEEPSEEK_MODEL`  | `deepseek-v4-flash`              |

## Agent planner (OpenAI-compatible chain)

Łańcuch buduje `build_agent_planner_endpoints()` — **kolejność w kodzie**:

1. **DeepSeek** — `DEEPSEEK_API_KEY` (priorytet #1, DEEPSEEK-MIGRATION-1); model `DEEPSEEK_MODEL` (domyślnie `deepseek-v4-flash`); base URL `DEEPSEEK_BASE_URL` (domyślnie `https://api.deepseek.com`); thinking mode + `reasoning_effort` przez `extra_body` na wywołaniu `chat.completions.create`
2. **Cerebras** — `AGENT_CEREBRAS_API_KEY` lub `CEREBRAS_API_KEY`; model `AGENT_CEREBRAS_MODEL` / `CEREBRAS_MODEL` / `AGENT_MODEL`
3. **NVIDIA** — `AGENT_NVIDIA_API_KEY` lub `NVIDIA_API_KEY`; model `AGENT_NVIDIA_MODEL` / `NVIDIA_MODEL` (domyślnie `meta/llama-3.3-70b-instruct`)
4. **Groq** — `AGENT_GROQ_API_KEY` lub `GROQ_API_KEY`; model `AGENT_GROQ_MODEL` / `GROQ_MODEL` / `AGENT_MODEL`
5. **OpenRouter** — `AGENT_OPENAI_API_KEY` + `AGENT_OPENAI_BASE_URL` (OpenAI-compatible); model `AGENT_MODEL`
6. **OpenRouter fallback** — ten sam endpoint, model `AGENT_MODEL_FALLBACK` (jeśli niepusty)
7. **Native OpenAI** — opcjonalnie `AGENT_OPENAI_NATIVE_API_KEY`
8. **Cursor** — opcjonalnie `AGENT_CURSOR_API_KEY`

Endpointy bez skonfigurowanego klucza są pomijane. Gdy żaden nie pasuje, używany jest
sam `AGENT_OPENAI_API_KEY` jako `openai_compat`.

**Semantyka porażki DeepSeek (pozycja 1) różni się celowo od pozostałych pozycji**:
rate limit, timeout, transient/5xx oraz auth/config failure (`401/403`, invalid/expired key)
przechodzą do Cerebras (pozycja 2) jako jawny degraded primary-provider state. Błędny request
wynikający z naszego kodu (`400`, `unsupported parameter`, `tool_choice`, malformed payload)
jest fail-fast. Reguła "non-retryable przerywa cały łańcuch" dla Cerebras/NVIDIA/Groq/...
pozostaje niezmieniona. Halucynacja (nieznane narzędzie) nadal zawsze przerywa natychmiast,
niezależnie od pozycji — to nie jest kwestia dostępności providera.

## Rate limiting i retry

- Semafor **2 równoczesnych calli** per provider (`central_llm_stage._get_provider_semaphore`).
- Przekroczenie → `LLMRateLimitError` → retry z exponential backoff (max 3).
- Structured: `LLM_CLIENT_TIMEOUT_SEC=30`, `LLM_HARD_TIMEOUT_SEC=60` (stałe w kodzie,
  **nie** per-stage 30/45/60 jak w starej dokumentacji).

## Konfiguracja env (skrót)

| Zmienna                               | Rola                           | Domyślna wartość                        |
| ------------------------------------- | ------------------------------ | --------------------------------------- |
| `DEEPSEEK_API_KEY`                    | Priorytet #1 (structured+agent)| —                                       |
| `DEEPSEEK_API_KEYS`                   | Multi-key pool                 | —                                       |
| `DEEPSEEK_MODEL`                      | Model DeepSeek                 | `deepseek-v4-flash`                     |
| `DEEPSEEK_BASE_URL`                   | Endpoint OpenAI-compatible     | `https://api.deepseek.com`              |
| `DEEPSEEK_THINKING_ENABLED`           | Thinking mode toggle           | `true`                                  |
| `DEEPSEEK_REASONING_EFFORT`           | Poziom wysiłku reasoningu      | `high`                                  |
| `GROQ_API_KEY`                        | Structured primary (typowo)    | —                                       |
| `GROQ_MODEL`                          | Model Groq structured          | `openai/gpt-oss-120b`                   |
| `CEREBRAS_API_KEY`                    | Fallback / alternacja / agent  | —                                       |
| `CEREBRAS_MODEL`                      | Model Cerebras                 | `openai/gpt-oss-120b`                   |
| `NVIDIA_API_KEY`                      | Fallback / agent               | —                                       |
| `NVIDIA_MODEL`                        | Model NVIDIA                   | `meta/llama-3.3-70b-instruct`           |
| `LLM_PRIMARY_PROVIDER`                | Override primary               | z `LLM_BACKEND` (`groq`)                |
| `LLM_FALLBACK_PROVIDERS`              | Lista fallbacków               | pusta                                   |
| `LLM_STRUCTURED_PROVIDER_ALTERNATION` | Rotacja groq↔cerebras          | `1` (gdy klucze OK)                     |
| `ANTHROPIC_API_KEY`                   | Nadpisuje primary na Anthropic | —                                       |
| `ANTHROPIC_MODEL`                     | Model Anthropic                | `claude-sonnet-4-20250514`              |
| `ANTHROPIC_BASE_URL`                  | `llm_client.py`                | `https://api.anthropic.com/v1/messages` |
| `ANTHROPIC_VERSION`                   | Nagłówek API                   | `2023-06-01`                            |
| `AGENT_MODEL`                         | Agent planner model            | `gpt-4o-mini`                           |
| `AGENT_MODEL_FALLBACK`                | Drugi model OpenRouter         | `""` (pusty)                            |
| `AGENT_MAX_ROUNDS`                    | Max tur agenta                 | `12`                                    |
| `AGENT_CEREBRAS_API_KEY`              | Agent Cerebras                 | → `CEREBRAS_API_KEY`                    |
| `AGENT_GROQ_API_KEY`                  | Agent Groq                     | → `GROQ_API_KEY`                        |
| `AGENT_NVIDIA_API_KEY`                | Agent NVIDIA                   | → `NVIDIA_API_KEY`                      |
| `AGENT_OPENAI_API_KEY`                | OpenRouter / compat            | —                                       |
| `AGENT_OPENAI_BASE_URL`               | Base URL compat                | `https://api.openai.com/v1`             |
