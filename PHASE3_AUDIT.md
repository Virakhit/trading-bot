# Phase 3 audit

- Baseline commit: `c0ff711dfa11c7d2ddd15d5318b273705aeb65b3` (`HEAD == origin/main` before this repair).
- Final repair commit: `c0862f2caa82197f68622f544ba6f7e0221cbb44`; tree is clean after commit.
- Phase 3 evidence remains green: 22 tests pass; the complete local suite after the repair is 117 passed, 2 skipped.
- Phase 1 financial invariant is unchanged: starting cash `10000.000`, gross realized P&L `-11.202`, fees `0.200`, net P&L `-11.402`, ending equity `9988.598`, total slippage `1.202`.
- Webull SDK is pinned to `webull-openapi-python-sdk==3.0.0`. Writes are structurally restricted to `region=th`, `environment=test`, `th-api.uat.webullbroker.com`, and `th-events-api.uat.webullbroker.com`, with account attestation before writes.
- Durable submit/cancel commit `PREPARED` before network transport. Ambiguous outcomes remain `UNKNOWN`; recovery queries by persistent client order ID and applies executions through `BrokerExecutionService` before any status transition. A terminal broker status without executions remains unresolved and never fabricates accounting.
- REST history preserves complete broker pages and rejects repeated cursors. Event callbacks normalize into a queue, preserve unmapped `LOCAL_MISSING` records, and use the persistent resolver after restart.
- The repaired automated pipeline is exercised end-to-end with MockBroker: strategy → persisted signal/snapshot/risk → durable submit → partial/final fills → exit → duplicate replay → reconciliation, with exact cash and position accounting.
- Incremental worker checkpoints are persisted in migration `0009`; worker restart and append-only replay are covered. XNYS session checks cover DST, holidays, and an early close.
- `reconcile` is now an actual bounded read-only Webull TEST operation when a persisted TEST account and credentials are present. `doctor` reports safe status and the TEST status command does not require credentials for local diagnostics.
- External read-only REST was attempted against the fixed TEST host and returned HTTP 401 invalid credentials; Events and TEST order were not run. Docker runtime was not run because Docker is unavailable on this host; CI now includes a Docker build step.
- Production execution remains impossible: no live mode, no production endpoint, no production CLI, no fallback, and `WebullExecutionEngine` fails closed.
