# Phase 2 repaired audit

Historical Phase 2 established the asynchronous broker lifecycle with MockBroker. The repaired baseline targets only `region=th`, `environment=test`; former US sandbox wording is obsolete.

- Phase 1 is unchanged: net P&L `-11.402`, ending equity `9988.598`.
- Phase 2 tests: `29 passed` on 2026-09-12.
- History reconciliation is bounded and paginated.
- Fill-before-ACK and late ACK do not regress state.
- Cancel/fill races preserve fills; final fills may supersede cancellation.
- Late fills are journaled and accounted once.
- Duplicate events/executions are idempotent; conflicts fail closed.
- `SUBMITTING` orders cannot be blindly retried.

Phase 2 made no Webull submission. Phase 3 adds the official Thailand TEST adapter and durable commands.
