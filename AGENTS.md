# AGENTS.md — gmail-agent

**Status:** aktywny router repo.

## Rola

`gmail-agent` jest Node B. Odpowiada za intake Gmail/Drive, journal i replay, mailbox/case runtime, engagement state, policy, execution, operational feed oraz konsumpcję feedbacku.

Nie jest właścicielem HVAC pricing, sizing ani `OfferDTO`; to należy do `kalk-top`.

## Czytaj najpierw

1. root `../AGENTS.md`;
2. `../knowledge/INDEX.md` i `../knowledge/source-of-truth.md`;
3. `../knowledge/memory/OPERATOR_DECISIONS.md` oraz `ACTIVE_WORKSPACE.md`;
4. `README.md`;
5. `docs/core/CONSTITUTION_V2_1.md`;
6. `docs/core/PROJECT_README.md`;
7. `docs/runbooks/LAST_PROVEN_STATE.md` tylko dla proof/runtime claims;
8. aktualny kod i targeted tests.

Nie czytaj historycznych handoffów, proof-packów, archiwów ani raw exports jako aktywnej prawdy.

## Runtime boundaries i freeze

- Domyślnie local Docker only; VPS/prod są zawieszone.
- Daszek jest projection-only; Node B pozostaje SoT spraw i wykonania.
- Zachowaj semantykę `message_id`, `signal_id`, `engagement_id`, `case_id`, `run_id`, `trace_id` i `source_signal_ids`.
- Zachowaj `POSTGRES_ATOMIC_MUTATION_CONFIRMED`; nie wracaj do stale full-row overwrite.
- `/tasks*` write routes pozostają fail-closed.
- Zachowaj stabilny `decision_key` oraz rozdzielenie `accepted`, execution, completion i convergence.
- Nie ponawiaj automatycznie `outcome_unknown`.
- Nie przywracaj finalnego sukcesu UI przed matching fresh projection.
- Brak autonomicznego customer email, Calendar write lub CRM write bez policy/HITL i osobnego proofu.

## Verification

Używaj targeted tests podczas implementacji. Pełny closeout obejmuje odpowiedni suite, workspace gate oraz — dla kodu bake’owanego — rebuild/recreate, host/container parity i health.

```powershell
python -m compileall tools/gmail_audit scripts -q
python -m pytest tools/gmail_audit/tests -q --tb=line
python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --verbose
```

Nie uruchamiaj realnych replayów, customer send ani operacji VPS bez jawnego zakresu.
