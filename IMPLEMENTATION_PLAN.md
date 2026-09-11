# Foundation implementation plan

1. Inspect repository and local Python runtime; verify official Webull documentation.
2. Define validated bars, signals, orders and paper-only configuration.
3. Implement deterministic strategy, CSV provider, risk controls and IOC execution.
4. Maintain explicit cash/position state and immutable strategy registrations.
5. Migrate relational journal; connect per-bar atomic lifecycle and trade metrics.
6. Add CLI and deterministic unit/integration checks.
7. Execute a demo; independently reconcile fills, cash, positions and trade lineage.
8. Document assumptions and intentionally unsupported integration features.

The initial checkout was empty. No production adapter or external account connection is part of this work.
