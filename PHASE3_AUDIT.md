# Phase 3 audit

- Starting/final base HEAD: `77b9b5b795adf86106341d25658d874e9128cde4` (`HEAD == origin/main` at baseline).
- Final tree: DIRTY by design; changes are present in the working tree and were not committed by this task.
- Phase 1 remains `10000.000` cash, `-11.202` gross, `0.200` fees, `-11.402` net, `9988.598` equity, `1.202` slippage.
- Phase 2: 29 tests pass. Phase 3: 22 tests pass.
- Official SDK: `webull-openapi-python-sdk==3.0.0`; Thailand TEST REST `th-api.uat.webullbroker.com`; Events `th-events-api.uat.webullbroker.com`.
- Writes require exact account attestation and are structurally limited to `region=th`, `environment=test`, and the two literal UAT hosts.
- Durable submit/cancel use PREPARED → SENDING → response transaction. Ambiguous outcomes remain UNKNOWN and never blind-retry.
- UNKNOWN recovery is fill-driven: executions are ingested before terminal state; a FILLED/PARTIALLY_FILLED snapshot without sufficient executions remains unresolved.
- Client-order resolution is backed by `broker_orders`; stream parsing raises `LOCAL_MISSING` for an unknown client ID.
- SDK history pages are preserved whole; repeated cursors are rejected by reconciliation.
- Events are normalized off the callback boundary and accounting remains in `BrokerExecutionService`.
- Network-free crash, duplicate, partial-fill, late-ACK, cancel-race, pagination, payload-conflict, and restart tests pass.
- External REST/Events/order calls: NOT TESTED successfully; no TEST credentials were available. A previous placeholder probe returned 401.
- Production order execution is impossible: no live mode, no production endpoint, no production CLI, and no strategy-to-production path.
