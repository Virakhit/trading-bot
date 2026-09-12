# Final system audit

## Result

Phase 1 deterministic research, Phase 2 broker lifecycle, Phase 3 Webull TEST safety, and the local automated paper/MockBroker pipeline are complete. Production order execution is impossible by configuration and endpoint design. The configured external Webull credentials were rejected by the fixed TEST host with HTTP 401; no write was attempted.

## Baseline and financial invariant

The repair started from `c0ff711dfa11c7d2ddd15d5318b273705aeb65b3`, equal to `origin/main`, with a clean tree. It is committed as `c0862f2caa82197f68622f544ba6f7e0221cbb44`. Baseline evidence was compile PASS, 113 passed/2 skipped, Phase 2 29 passed, Phase 3 22 passed, system automation 12 passed, and lifecycle `verified=true`. The Phase 1 invariant remains:

`10000.000 - 2010.602040 + 1999.400040 - 0.200 = 9988.598`

Gross realized P&L is `-11.202`, fees are `0.200`, net P&L is `-11.402`, ending cash/equity is `9988.598`, and total slippage is `1.202`. A fresh migrated SQLite database reproduced 11 signals, two closed trades, `-11.402` net P&L, and `9988.598` equity; `scripts.verify_lifecycle` returned `verified=true`.

## Architecture

The supported flow is strategy → normalized market snapshot → persisted signal → risk decision → `BrokerOrder` → durable command → local paper, deterministic MockBroker, or Webull Thailand TEST → normalized asynchronous events/fills → exact cash/position accounting → reconciliation and metrics. Strategy versions persist configuration, configuration hash, source/code hash, and Git SHA when available. Signals and broker orders retain version and risk foreign keys; changing a strategy configuration under an existing version raises and cannot mutate history.

`AutomatedOrchestrator.run_broker` uses `AsyncBrokerOrchestrator`; it does not fall back to `PaperExecutionEngine`. `local-paper` remains the default. `mock-broker` is deterministic and network-free. `webull-th-test` requires `AUTOMATED_WEBULL_TEST_ENABLED=true`, exact TEST hosts, and an attested TEST account. There is no `live`, production, or production read/write fallback.

## Broker lifecycle and safety

Commands are durable before transport. UNKNOWN recovery resolves by client order ID, persists broker IDs, ingests all available executions through `BrokerExecutionService`, and only then applies safe status. Missing execution history leaves a safety mismatch. `PersistentOrderResolver` maps client IDs from `broker_orders` across process restart. Webull event callbacks enqueue normalized events, preserve unknown mappings as auditable `LOCAL_MISSING`, and expose reconnect/health state; reconciliation performs REST gap recovery with full-page cursor handling and repeated-cursor detection. Duplicate events and execution IDs are idempotent; conflicting payloads fail closed.

Only fills mutate broker cash, positions, weighted average basis, realized P&L, and per-fill fees. Pending BUY exposure and pending SELL oversubscription are included in risk checks. Partial fills cannot duplicate positions, overfill orders, oversell, or double-count P&L. Pause and kill-switch controls persist in the database; broker recovery and reconciliation continue while new orders are blocked. The worker migrates at startup, recovers commands, processes only new feed checkpoints, reconciles periodically, and handles SIGINT/SIGTERM.

## Market data, sessions, and research

CSV, replay, and synthetic feeds normalize to `MarketSnapshot` and persist quality failures. Non-finite/non-positive prices, timestamp regression, duplicate timestamps, stale observations, and invalid ranges are rejected without interpolation. `SessionCalendar(exchange="XNYS")` uses `exchange_calendars` for Webull TEST and has DST, holiday, and early-close coverage; local sessions remain configurable. `app.analytics.backtest` and chronological walk-forward splitting provide deterministic evaluation without look-ahead. Existing strategy behavior and losing baseline results were not tuned.

## Database, deployment, and CI

Alembic migration `0009_market_checkpoints` adds durable worker cursors. Empty-database upgrade and SQLAlchemy metadata parity pass; no behavior depends on `create_all`. SQLite is tested; PostgreSQL remains unclaimed because it was not exercised. Docker runs as a non-root user, copies `alembic.ini`, has a health check, and defaults to local paper; CI now builds the image while all Webull jobs remain opt-in and credential-free.

Safe CLI commands include `doctor`, `status`, `positions`, `orders`, `fills`, `commands`, `reconcile`, `pause`, `resume`, `kill-switch`, and `webull-test-status`. `reconcile` is read-only and bounded. Logs use safe IDs and the scanner checks `.env`, logs, and common token/signature patterns; no secrets are committed.

## Evidence and limitations

- Final local suite: **117 passed, 2 skipped** (the skips are opt-in Webull integration tests).
- Compile, Phase 1 audit, Phase 2, Phase 3, system automation, migration parity, lifecycle verification, secret scan, and deterministic MockBroker E2E: PASS.
- External read-only Webull TEST: **FAIL**; fixed TEST host returned HTTP 401 invalid credentials.
- External Webull TEST order: **NOT TESTED**; credentials unavailable.
- Docker build/runtime: configuration and CI step added; local Docker runtime **NOT TESTED** because Docker is unavailable.
- Webull SDK: `webull-openapi-python-sdk==3.0.0`.
- Environment: Thailand TEST only.

Production execution possible: **NO**.
