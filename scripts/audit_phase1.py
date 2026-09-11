"""Independent Decimal audit of the existing demo; no accounting helpers reused."""
import hashlib
import json
from collections import Counter
from decimal import Decimal as D
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database.models import (FillRow, OrderRow, PositionRow, RiskRow, Run, SignalRow,
                                 Snapshot, StrategyRow, StrategyVersion, Trade, TradeMetrics)


def equal(left, right) -> None:
    assert abs(D(str(left)) - D(str(right))) < D("0.00000001"), (left, right)


def audit(session: Session, run_id: str) -> dict:
    run = session.get(Run, run_id)
    version = session.get(StrategyVersion, run.version_id)
    strategy = session.get(StrategyRow, version.strategy_id)
    encoded = json.dumps(version.config, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert hashlib.sha256(encoded).hexdigest() == version.config_hash
    signals = list(session.scalars(select(SignalRow).where(SignalRow.run_id == run.id).order_by(SignalRow.timestamp)))
    details, snapshots, decisions = [], {}, {}
    for signal in signals:
        snapshot = session.scalar(select(Snapshot).where(Snapshot.signal_id == signal.id))
        risk = session.scalar(select(RiskRow).where(RiskRow.signal_id == signal.id))
        assert snapshot and risk and signal.version_id == version.id
        assert signal.payload["strategy_id"] == version.strategy_id
        assert signal.payload["strategy_version"] == version.version
        assert snapshot.payload["timestamp"] == signal.payload["timestamp"]
        snapshots[signal.id], decisions[signal.id] = snapshot, risk
        orders = list(session.scalars(select(OrderRow).where(OrderRow.signal_id == signal.id)))
        fills = list(session.scalars(select(FillRow).join(OrderRow).where(OrderRow.signal_id == signal.id)))
        category = "other"
        if fills:
            category = "executed"
            assert risk.status == "APPROVED"
        elif signal.action == "HOLD" and risk.codes == ["NO_ACTION"]:
            category = "HOLD/no action"
        elif set(risk.codes) & {"PROTECTIVE_EXIT_PENDING", "END_OF_DATA_ENTRY_DISABLED"}:
            category = "duplicate/prevented"
        elif risk.status == "REJECTED":
            category = "rejected by risk"
        details.append({"signal_id": signal.id, "timestamp": signal.payload["timestamp"], "action": signal.action,
                        "category": category, "snapshot_id": snapshot.id, "risk_id": risk.id,
                        "risk_status": risk.status, "reason_codes": risk.codes,
                        "order_ids": [o.id for o in orders], "fill_ids": [f.id for f in fills]})
    trades = list(session.scalars(select(Trade).where(Trade.run_id == run.id).order_by(Trade.created_at)))
    total_buy = total_sell = total_fees = total_slip = D(0)
    traces = []
    for trade in trades:
        assert trade.status == "CLOSED" and trade.version_id == version.id
        position = session.get(PositionRow, trade.position_id)
        assert position.run_id == run.id
        rows = session.execute(select(FillRow, OrderRow).join(OrderRow, FillRow.order_id == OrderRow.id)
                               .where(FillRow.trade_id == trade.id).order_by(FillRow.timestamp, FillRow.created_at)).all()
        # This independent audit intentionally targets the demo's two one-entry/one-exit trades.
        assert len(rows) == 2
        (entry, entry_order), (exit_fill, exit_order) = rows
        assert entry_order.payload["side"] == "BUY" and exit_order.payload["side"] == "SELL"
        assert entry.quantity == exit_fill.quantity
        assert entry_order.signal_id == trade.entry_signal_id and exit_order.signal_id == trade.exit_signal_id
        quantity = entry.quantity
        buy, sell = D(str(entry.price)) * quantity, D(str(exit_fill.price)) * quantity
        fees = D(str(entry.fee)) + D(str(exit_fill.fee))
        steps = []
        slippages = []
        for fill, order in rows:
            signal = session.get(SignalRow, order.signal_id)
            assert fill.position_id == trade.position_id and signal.version_id == version.id
            assert signal.symbol == position.symbol == order.payload["symbol"]
            assert decisions[signal.id].status == "APPROVED"
            assert order.payload["quantity"] == fill.quantity
            equal(fill.fee, D(str(run.settings["fee_per_share"])) * fill.quantity)
            equal(fill.timestamp.timestamp(), signal.timestamp.timestamp())
            snapshot = snapshots[signal.id]
            side = order.payload["side"]
            quote = D(str(snapshot.payload["ask" if side == "BUY" else "bid"]))
            impact = D(str(run.settings["slippage_bps"])) / 10000
            equal(fill.price, quote * (1 + impact if side == "BUY" else 1 - impact))
            reference = D(str(signal.payload["signal_price"]))
            slip = (D(str(fill.price)) - reference) * fill.quantity * (1 if side == "BUY" else -1)
            slippages.append(slip)
            steps.append({"signal_id": signal.id, "snapshot_id": snapshot.id, "risk_id": decisions[signal.id].id,
                          "order_id": order.id, "fill_id": fill.id, "position_id": position.id,
                          "side": side, "quantity": fill.quantity, "signal_price": float(reference),
                          "order_type": order.payload["order_type"], "requested_price": order.payload["limit_price"],
                          "bid": snapshot.payload["bid"], "ask": snapshot.payload["ask"],
                          "fill_price": fill.price, "fee": fill.fee, "slippage": float(slip)})
        price_sequence = [s for s in signals if entry.timestamp <= s.timestamp <= exit_fill.timestamp]
        samples = []
        for sig in price_sequence:
            close = D(str(snapshots[sig.id].payload["close"]))
            samples.append({"timestamp": sig.payload["timestamp"], "price": float(close), "kind": "close",
                            "unrealized_pnl": float((close - D(str(entry.price))) * quantity)})
        samples.append({"timestamp": session.get(SignalRow, exit_order.signal_id).payload["timestamp"],
                        "price": exit_fill.price, "kind": "exit_fill", "unrealized_pnl": float(sell - buy)})
        independent = {"gross_pnl": sell - buy, "fees": fees, "net_pnl": sell - buy - fees,
                       "entry_slippage": slippages[0], "exit_slippage": slippages[1], "total_slippage": sum(slippages),
                       "mae": min([0] + [s["unrealized_pnl"] for s in samples]),
                       "mfe": max([0] + [s["unrealized_pnl"] for s in samples])}
        metrics = session.scalar(select(TradeMetrics).where(TradeMetrics.trade_id == trade.id))
        for key, value in independent.items():
            equal(metrics.payload[key], value)
        equal(trade.payload["entry_cost"], buy)
        equal(trade.payload["exit_value"], sell)
        equal(trade.payload["fees"], fees)
        equal(metrics.payload["maximum_unrealized_loss"], independent["mae"])
        equal(metrics.payload["maximum_unrealized_profit"], independent["mfe"])
        traces.append({"trade_id": trade.id, "version_id": version.id, "position_id": position.id,
                       "metrics_id": metrics.id, "status": trade.status, "steps": steps, "price_sequence": samples,
                       "independently_recalculated": {k: float(v) for k, v in independent.items()}})
        total_buy += buy
        total_sell += sell
        total_fees += fees
        total_slip += sum(slippages)
    positions = list(session.scalars(select(PositionRow).where(PositionRow.run_id == run.id)))
    assert all(p.quantity == 0 and p.average_entry == 0 and p.unrealized_pnl == 0 for p in positions)
    equal(run.cash, D(str(run.settings["initial_cash"])) - total_buy + total_sell - total_fees)
    equal(run.equity, run.cash)
    categories = dict.fromkeys(["executed", "rejected by risk", "HOLD/no action", "duplicate/prevented", "other"], 0)
    categories.update(Counter(s["category"] for s in details))
    return {"verified": True, "run_id": run.id, "status": run.status,
            "strategy": {"strategy_id": strategy.id, "name": strategy.name, "strategy_version": version.version,
                         "version_id": version.id, "configuration": version.config, "configuration_hash": version.config_hash,
                         "git_sha": version.git_sha, "code_hash": version.code_hash, "source_available": version.source_code is not None},
            "signal_categories": categories, "signals": details, "trades": traces,
            "reconciliation": {"starting_cash": run.settings["initial_cash"], "entry_cost": float(total_buy),
                               "exit_proceeds": float(total_sell), "gross_realized_pnl": float(total_sell - total_buy),
                               "fees": float(total_fees), "slippage_already_in_fill_prices": float(total_slip),
                               "net_pnl": float(total_sell - total_buy - total_fees), "ending_cash": run.cash, "ending_equity": run.equity}}


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    from app.config import Settings
    from app.database import engine_for
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    with Session(engine_for(Settings().database_url)) as session:
        report = json.dumps(audit(session, args.run_id), indent=2)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
    else:
        print(report)
