# Signal Runtime Operator

Cel: obsługa signal runtime na Node B.

## Standard run

1. Preflight (`doctor`).
2. `signal-run` bounded.
3. `signal-worker` bounded lub loop kontrolowany.
4. Readback artefaktow i statusu.

## Obowiazkowe metryki

- processed,
- failed,
- stop_reason,
- last_errors / item_failures,
- last_error_summary.
