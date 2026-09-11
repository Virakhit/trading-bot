# Phase 1 final audit

Verdict: **READY for Phase 2 planning/development within the paper-only boundary.** Phase 2 was not started.

## Scope and evidence

Original run: `4e068832-ca98-4a8d-aefe-97ad15b6a180` in `data/research.db`. Fresh run: `3d598f84-dbfe-4aaa-9e3c-e9bb4b3c8f75` in `data/phase1-clean-audit.db`.

Full suite: **50 passed** (28 existing plus 22 added parametrized cases). Independent audit arithmetic uses Decimal and does not call production portfolio, execution or metric calculation helpers.

Artifacts: [original audit](data/phase1-original-audit.json), [fresh audit](data/phase1-clean-audit.json), [history preservation](data/phase1-history-preservation.json). Original database backup: `data/phase1-before-audit.db`. No original trading history was rewritten.

## 1. Exact P&L reconciliation

| Item | Trade 1 | Trade 2 | Total |
| --- | ---: | ---: | ---: |
| Buy quantity | 10 | 10 | 20 |
| Entry signal price | 101 | 100 | |
| Buy fill price | 101.030202 | 100.030002 | |
| Entry cash cost before fee | 1010.302020 | 1000.300020 | 2010.602040 |
| Exit signal price | 99 | 101 | |
| Sell fill price | 98.970202 | 100.969802 | |
| Exit proceeds before fee | 989.702020 | 1009.698020 | 1999.400040 |
| Gross realized P&L | -20.600000 | 9.398000 | -11.202000 |
| Entry fee | 0.050000 | 0.050000 | 0.100000 |
| Exit fee | 0.050000 | 0.050000 | 0.100000 |
| Total fees | 0.100000 | 0.100000 | 0.200000 |
| Entry slippage | 0.302020 | 0.300020 | 0.602040 |
| Exit slippage | 0.297980 | 0.301980 | 0.599960 |
| Total slippage, already in fills | 0.600000 | 0.602000 | 1.202000 |
| Net P&L | -20.700000 | 9.298000 | -11.402000 |

`10000 - 2010.602040 + 1999.400040 - 0.200000 = 9988.598000`.

`9988.598 - 10000 = -11.402 = -11.202 gross realized - 0.200 fees`.

The hypothetical P&L at signal prices was -10.000. Execution shortfall of 1.202 reduces it to -11.202. Slippage is already in the gross result; subtracting it again is incorrect. Ending cash and equity both equal 9988.598 because all positions are flat. Fills, trade aggregates, trade metrics and the run cash/equity all reconcile within the documented 1e-8 storage tolerance. No historical P&L discrepancy was found.

## 2. Full trade traceability

### Trade 1

Closed trade ID: `79fddc21-2a28-4492-ba8a-c2c38c7daaf5`. Strategy-version row: `c6e79ee1-767e-481f-b4c5-df74464d6eeb`. Explicit position: `c8e830a2-f633-4916-a855-1886e6ba65bf`. Metrics row: `aa19032b-1f38-402a-a8ee-c0d0d63a9e22`.

| Lifecycle link | Entry | Exit |
| --- | --- | --- |
| Signal | `312051d1-41b9-4d1e-be93-df8cb245bc1b` | `125672c1-b490-4b1c-ab33-0dd55430b5fb` |
| Market snapshot | `124c18a2-7ac8-4c1e-a52e-5d3e0a97c78a` | `b1bd3b8b-8111-4792-90ef-b02692cbace1` |
| Risk decision (APPROVED) | `cde23f3a-8579-4f4a-9334-736598eb0876` | `4f99cac3-1448-47b4-8137-df604f2c1645` |
| Order | `67235215-38f8-421b-a2d5-491e4756f4f0` | `c7bb8bf3-db07-45be-bc8c-13464900e21d` |
| Fill | `971a2f12-23fd-4590-98e2-cbd524e0b318` | `8ffc1dd2-58d7-4e77-9f3a-44c63a061188` |

Each fill has foreign keys to its order, trade and position. The order references the signal; the signal references the run and strategy version. Snapshots/risk decisions each reference their signal. The closed trade references its entry and exit signals, and trade metrics reference the closed trade.

### Trade 2

Closed trade ID: `72af4adf-7e13-42ed-9c00-370ef18b3b02`. Strategy-version row: `c6e79ee1-767e-481f-b4c5-df74464d6eeb`. Explicit position: `c8e830a2-f633-4916-a855-1886e6ba65bf`. Metrics row: `10871081-ee1b-4a74-a864-7a45fed1444d`.

| Lifecycle link | Entry | Exit |
| --- | --- | --- |
| Signal | `81c4411a-0887-4021-b8b9-4ac3e5a74dbb` | `c72c006e-6130-4a44-ac20-518df950301c` |
| Market snapshot | `d5b75f31-5a09-4db9-82e6-10429cf7dc53` | `04a8a543-e180-4d27-8d03-9da9b1cb893c` |
| Risk decision (APPROVED) | `bc859724-b24a-4f2c-929e-264917faa3bc` | `729a9457-8abe-41b8-b425-731e4dcaf184` |
| Order | `4f255730-4df5-4cfa-a8b1-cfff0d7465b6` | `d6dfd645-28e4-47dc-b2b4-9bfbb0638078` |
| Fill | `8263bb65-f384-47ea-9b23-c64b628691d6` | `25f0e1a9-f1a6-4798-94d7-6224034e1bdd` |

Each fill has foreign keys to its order, trade and position. The order references the signal; the signal references the run and strategy version. Snapshots/risk decisions each reference their signal. The closed trade references its entry and exit signals, and trade metrics reference the closed trade.

The position row is intentionally shared across successive trades in the same run/symbol; each trade and fill has its own stable ID. It is an explicit current holding, not a position inferred solely from orders.

## 3. Strategy-version integrity

Strategy: `demo-momentum` / `Three-bar momentum`. Version: `1.0.0`.

Configuration: `{"warmup_bars": 3, "timeframe": "1m"}`.

Canonical SHA-256: `99f29d8e806e6320546f126114eb14d1300e6f79c67300047a59c871f3fffca9`.

All 11 signals and both closed trades join to this exact version/configuration; the hash was independently recomputed. Changed configuration with the same version fails registration. A new version leaves the entire old audit result unchanged. A detached JSON snapshot now also protects against mutation of a shared nested configuration object before transaction commit.

The original version has `git_sha=null` and no source snapshot because it predates the Git checkout/source-column population. This absence is reported, not backfilled or fabricated. Its source hash is retained. The fresh version stores a source snapshot and the available Git HEAD: `6ad9f3ac1ee2fb969dee773f181d7e028f5d12cf`. Git HEAD identifies the base commit; uncommitted audit edits are not represented by that commit. Archive the working source and dependency lock with research results.

## 4. All 11 signals

| UTC timestamp | Action | Category | Reason | Signal ID |
| --- | --- | --- | --- | --- |
| 2026-01-05T14:30:00Z | HOLD | HOLD/no action | NO_ACTION | `ee886cc4-62b8-4b5d-a255-f7f6090236c8` |
| 2026-01-05T14:31:00Z | HOLD | HOLD/no action | NO_ACTION | `7f24cf00-0f4f-409c-b7cc-87fe46c54b78` |
| 2026-01-05T14:32:00Z | BUY | executed | APPROVED | `312051d1-41b9-4d1e-be93-df8cb245bc1b` |
| 2026-01-05T14:33:00Z | HOLD | HOLD/no action | NO_ACTION | `52128b78-3b3e-4603-a268-6f2b88373062` |
| 2026-01-05T14:34:00Z | HOLD | HOLD/no action | NO_ACTION | `d242e010-9319-452a-9670-f57122360859` |
| 2026-01-05T14:35:00Z | EXIT | executed | APPROVED | `125672c1-b490-4b1c-ab33-0dd55430b5fb` |
| 2026-01-05T14:36:00Z | HOLD | HOLD/no action | NO_ACTION | `a151948e-dd6a-445d-9c5b-dd99610a552c` |
| 2026-01-05T14:37:00Z | BUY | executed | APPROVED | `81c4411a-0887-4021-b8b9-4ac3e5a74dbb` |
| 2026-01-05T14:38:00Z | HOLD | HOLD/no action | NO_ACTION | `d0cc98b6-0fd9-4197-aa9a-808c7f99067d` |
| 2026-01-05T14:39:00Z | HOLD | HOLD/no action | NO_ACTION | `be4fb02a-fab5-49c4-a566-cbb8b1db830a` |
| 2026-01-05T14:40:00Z | EXIT | executed | APPROVED | `c72c006e-6130-4a44-ac20-518df950301c` |

Totals: executed 4 (2 BUY + 2 EXIT); risk-blocked trading intents 0; HOLD/no action 7; duplicate/prevented 0; other 0. The legacy verifier called all seven HOLD decisions rejected because their stored status is REJECTED with NO_ACTION. They are not seven failed trading orders. Every decision and reason remains queryable; tests separately exercise actual kill-switch/risk rejections.

```sql
SELECT s.id, s.action, r.status, r.codes
FROM signals s JOIN risk_decisions r ON r.signal_id = s.id
WHERE s.run_id = '4e068832-ca98-4a8d-aefe-97ad15b6a180'
ORDER BY s.timestamp;
```

## 5. Independently recalculated MAE/MFE

All values are signed dollars on the then-open quantity, relative to the actual average entry price. Samples are saved bar closes and exit execution prices, not unobserved intrabar extrema.

### Trade 1 price sequence

| UTC timestamp | Sample | Price | Unrealized P&L |
| --- | --- | ---: | ---: |
| 2026-01-05T14:32:00Z | close | 101.000000 | -0.302020 |
| 2026-01-05T14:33:00Z | close | 103.000000 | 19.697980 |
| 2026-01-05T14:34:00Z | close | 102.000000 | 9.697980 |
| 2026-01-05T14:35:00Z | close | 99.000000 | -20.302020 |
| 2026-01-05T14:35:00Z | exit_fill | 98.970202 | -20.600000 |

Persisted and recalculated MAE = **-20.600000**, MFE = **19.697980**.

### Trade 2 price sequence

| UTC timestamp | Sample | Price | Unrealized P&L |
| --- | --- | ---: | ---: |
| 2026-01-05T14:37:00Z | close | 100.000000 | -0.300020 |
| 2026-01-05T14:38:00Z | close | 103.000000 | 29.699980 |
| 2026-01-05T14:39:00Z | close | 104.000000 | 39.699980 |
| 2026-01-05T14:40:00Z | close | 101.000000 | 9.699980 |
| 2026-01-05T14:40:00Z | exit_fill | 100.969802 | 9.398000 |

Persisted and recalculated MAE = **-0.300020**, MFE = **39.699980**.

New regressions include a profitable trade with a meaningful adverse move (-20.1 MAE / 49.9 MFE) and a losing trade with a favorable move (-20.2 MAE / 29.9 MFE), in addition to the exact two-demo-trade audit.

## 6-7. Slippage and fees

All four original orders are MARKET orders, so requested/limit price is null. Their signal price, bid/ask, fill price and exact IDs are in the JSON and tables above. Buy fill = ask × 1.0002; sell fill = bid × 0.9998. Entry slippage = (fill - signal) × quantity; exit slippage = (signal - fill) × quantity. Positive is execution cost; negative is improvement. Tests cover both signs and verify limit behavior.

The fee model charges 0.005 per filled share on each unique fill, both sides. Each original 10-share fill costs 0.05. Zero-fill cancelled/rejected orders cost zero. Multi-fill tests verify aggregate fees, cash and net P&L; duplicate deliveries add no fee. Invalid fee amounts are rejected before financial state changes. There is no minimum commission or regulatory/tax model.

## 8-9. Partial fills and position accounting

Tests verify partial-entry/exit quantities, one position row per run/symbol, duplicate-fill suppression, no overselling, no double P&L, complete liquidation and remaining cost basis. Multi-fill entry orders increment the daily entry-order counter once, not once per fill.

Exact requested example: BUY 2 @ 100 plus BUY 3 @ 110 gives quantity 5, cost 530, average 106. SELL 2 @ 120 realizes gross 28 and leaves quantity 3, average 106, basis 318. SELL 3 @ 100 realizes -18; total gross realized = 10, total fees = 0.05, ending cash = 10009.95, quantity/basis = 0.

## 10-11. Restart and idempotency

A subprocess runs the original demo through the entry, exits, and a new subprocess resumes its run ID. Reloaded quantity = 10 and cash = 8989.64798; daily entry count and opening equity are restored. Entry signal, fill, trade, position and strategy-version IDs survive. Resume ends at 9988.598 and zero quantity. Replaying the completed session changes no row counts or cash.

Additional tests interrupt after a committed entry and fail after fill calculation but before checkpoint commit. The latter rolls back fills and financial state; resumption from the committed cursor reproduces -11.402. Changed data/settings/quantity and corrupt cash fail closed. Replaying signals (including a new UUID for the same semantic decision), terminal order results and fills produces no duplicate state; conflicting identities fail explicitly.

Legacy pre-audit runs have no recovery checkpoint and remain read-only. Their history is preserved. Recovery requires the same complete CSV, configuration and quantity and resumes at the next committed cursor. It does not accept an unrelated data extension or reset an existing account. No broker streaming recovery was added.

## 12. Safety

Searched application, migrations, scripts, tests and dependency declarations for create_all, production Webull endpoints, order-submission clients and network libraries. No production endpoint, SDK trading client, HTTP order submission path or usable production mode exists. Runtime WebullExecutionEngine remains an unconditional explicit RuntimeError; the existing safety regression calls it and verifies failure. MODE accepts only paper. No strategy, Webull, Hermes or LLM integration was added.

## 13-14. Schema and clean database

Created a new empty `data/phase1-clean-audit.db`, applied migrations 0001 → 0002 → 0003, then compared with SQLAlchemy metadata: zero differences. SQLite foreign_key_check returned no errors. No application/test create_all call exists. Separate CLI invocations migrated, seeded data, ran the demo, listed zero holdings and calculated performance. Independent reconstruction from the fresh database reproduced the original financial results.

Compared every original column of all 13 historical application tables against the pre-migration backup: unchanged. Only migration metadata and added nullable checkpoint/decision-key plus revision columns changed.

## 15. Tests and fixes

Final suite: **50 passed**, **22 added test cases**. Added coverage: empty-schema comparison; exact fresh-demo accounting/category audit; both excursion patterns; requested weighted-basis example; fill retries/conflicts; stable execution identity; subprocess restart and replay; altered-input/corrupt-state refusal; signal/order replay; split-fill fees and duplicate suppression; invalid execution rollback; historical-version preservation; atomic oversell refusal; positive/negative slippage; zero-fill fees; interruption/checkpoint-failure recovery; detached nested config snapshots.

Found and fixed: (1) no runnable restart/recovery path, (2) no stable fill identity/idempotent result boundary, (3) entry-order count incremented per fill, and (4) registration retained a caller-owned mutable config object until serialization. Added validation for execution provenance, quantities and fees. No discrepancy in original P&L or MAE/MFE was found; no old trade result was changed.

## Readiness boundary

**READY** for the next paper-research phase. This audit does not certify live trading, asynchronous broker events, multi-process portfolio operation or PostgreSQL integration. Existing close-quote, sampled-excursion and floating-point simulation limitations remain documented. Phase 2 has not begun.
