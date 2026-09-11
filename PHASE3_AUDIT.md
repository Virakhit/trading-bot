# Phase 3 audit

1. **Starting HEAD:** `ef7aa8989ab001d80077a3402dc4ed86cc873a81`.
2. **Ending HEAD / tree:** HEAD unchanged; repairs are uncommitted.
3. **Phase 1 before/after:** cash `10000.000`, gross `-11.202`, fees `0.200`, net `-11.402`, equity `9988.598`, slippage `1.202`.
4. **Phase 2 repaired:** Thailand TEST baseline and 29 passing tests recorded.
5. **Architecture:** `WebullTestAdapter` normalizes official SDK responses.
6. **Migration:** `0006_scope_broker_identity` scopes IDs and aligns metadata.
7. **Durable submit:** PREPARED commit, SENDING commit, transport, response transaction.
8. **Durable cancel:** same boundary while order is CANCEL_PENDING.
9. **Crash evidence:** tests cover prepared-only, SENDING, accepted/lost response, rollback replay, ambiguous cancel, and inconclusive recovery.
10. **UNKNOWN:** absent or inconclusive query stays UNKNOWN.
11. **Retry:** only proven `BrokerDefinitelyNotSent` permits explicit retry.
12. **SDK:** official `webull-openapi-python-sdk==3.0.0`, pinned twice.
13. **Official docs:** Thailand Getting Started and API/Events environment pages.
14. **TEST hosts:** `th-api.uat.webullbroker.com`; `th-events-api.uat.webullbroker.com`.
15. **Account attestation:** exact list match, hashed reference, absent fails closed.
16. **Identifiers:** client/broker IDs account-scoped; event/execution IDs order-scoped.
17. **Statuses:** centralized mapping; unknown maps UNKNOWN.
18. **Order/fill mapping:** SDK payloads become Decimal domain models.
19. **Events:** official gRPC SDK isolated in `events.py`; callback does no accounting.
20. **Reconnect:** `recover_gap` replays REST recovery through the consumer.
21. **Reconciliation:** bounded history, orders, fills, cash, and positions.
22. **Fill payloads:** order, quantity, price, and fee conflicts become `PAYLOAD_MISMATCH`.
23. **Unit count:** `101 passed` in the final full suite; dedicated Phase 3: `22 passed`.
24. **Integration count:** two opt-in files.
25. **Skipped integration:** read and write flags are separate and off by default.
26. **Real TEST API:** NOT TESTED EXTERNALLY. A placeholder credential probe returned 401.
27. **TEST order:** none submitted.
28. **Files changed:** final `git status`; tracked SDK log removed.
29. **Limitations:** no valid TEST credentials; external REST/stream/write remain unverified. Write test skips until safe inputs are selected.
30. **Production safety:** production cannot be configured, hosts are literals, no production CLI or strategy route exists.
31. **Phase 4 readiness:** READY on local evidence; external TEST connectivity is an operational prerequisite.

Cash: `10000 - 2010.602040 + 1999.400040 - 0.200 = 9988.598`. Slippage is already in fill prices.
