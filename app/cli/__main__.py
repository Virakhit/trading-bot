import argparse
import asyncio
import csv
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import Settings
from app.core.runner import run_paper
from app.database import engine_for
from app.database.models import (BrokerAccount, FillRow, OrderRow, PositionRow, SignalRow, Trade, TradeMetrics,
                                 Run)
from app.market_data import CSVProvider
from app.strategies import MomentumStrategy
from app.ops.controls import ControlStore
from app.security import scan_repository
from app.brokers.webull import WebullTestConfig
from app.database.models import BrokerCommand
from app.brokers.webull.adapter import WebullTestAdapter
from app.brokers.resolver import PersistentOrderResolver
from app.brokers.service import BrokerExecutionService


def seed(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["symbol", "timestamp", "open", "high", "low", "close", "bid", "ask", "volume"])
        writer.writeheader()
        for i, price in enumerate([100, 100, 101, 103, 102, 99, 98, 100, 103, 104, 101]):
            writer.writerow(dict(symbol="DEMO", timestamp=(datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc) + timedelta(minutes=i)).isoformat(),
                                 open=price, high=price + 0.2, low=price - 0.2, close=price, bid=price - 0.01, ask=price + 0.01, volume=1000))


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic local paper research only")
    parser.add_argument("command", choices=["init-db", "seed-demo-data", "run-paper", "paper-run", "positions", "orders", "fills", "trades", "performance",
                                             "doctor", "status", "commands", "reconcile", "pause", "resume", "kill-switch", "webull-test-status"])
    parser.add_argument("--csv", default="data/demo.csv")
    parser.add_argument("--symbol", default="DEMO")
    parser.add_argument("--quantity", type=int, default=10)
    parser.add_argument("--run-id")
    parser.add_argument("--max-bars", type=int, help="Pause after this many additional bars; resume with --run-id and the same CSV")
    parser.add_argument("--network-check", action="store_true", help="Use the read-only Webull TEST account check")
    args = parser.parse_args()
    settings = Settings()
    Path("data").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(filename="logs/events.jsonl", level=logging.INFO, format="%(message)s")
    engine = engine_for(settings.database_url)
    controls = ControlStore(engine)
    if args.command == "doctor":
        findings = scan_repository(Path.cwd())
        print(json.dumps({"status": "UNSAFE" if findings else "OK", "mode": settings.mode,
                          "execution_backend": settings.execution_backend, "control": controls.get(),
                          "secret_findings": findings}, indent=2))
        return
    if args.command == "pause": print(controls.set("PAUSED", "operator")); return
    if args.command == "resume": print(controls.set("RUNNING", "operator")); return
    if args.command == "kill-switch": print(controls.set("KILL_SWITCH", "operator")); return
    if args.command == "webull-test-status":
        try:
            config = WebullTestConfig.from_env()
        except Exception:
            if args.network_check:
                raise
            print(json.dumps({"broker": "webull", "region": "th", "environment": "test",
                              "endpoint": "th-api.uat.webullbroker.com",
                              "events_endpoint": "th-events-api.uat.webullbroker.com",
                              "credentials_configured": False, "order_submission": "disabled"}, indent=2))
            return
        if not args.network_check:
            print(json.dumps({**config.diagnostics(), "credentials_configured": True,
                              "order_submission": "disabled"}, indent=2)); return
        from app.database.models import BrokerAccount
        account_id = None
        with Session(engine) as session:
            account = session.scalar(select(BrokerAccount).where(BrokerAccount.broker == "webull",
                                                                  BrokerAccount.environment == "test").order_by(BrokerAccount.created_at.desc()))
            account_id = account.id if account else None
        broker = WebullTestAdapter(config)
        asyncio.run(broker.attest_account())
        state = asyncio.run(broker.query_account_state())
        print(json.dumps({**config.diagnostics(), "credentials_configured": True,
                          "account_attested": True, "account_ref": state.account_ref,
                          "cash": str(state.cash), "positions": {k: str(v) for k, v in state.positions.items()},
                          "local_account_id": account_id}, indent=2)); return
    if args.command == "status":
        with Session(engine) as session:
            run = session.scalar(select(Run).order_by(Run.created_at.desc()))
            print(json.dumps({"control": controls.get(), "run_id": run.id if run else None,
                              "status": run.status if run else None}, indent=2))
        return
    if args.command == "commands":
        with Session(engine) as session:
            print(json.dumps([{"id": c.id, "order_id": c.order_id, "status": c.status} for c in session.scalars(select(BrokerCommand))], indent=2))
        return
    if args.command == "reconcile":
        with Session(engine) as session:
            account = session.scalar(select(BrokerAccount).where(BrokerAccount.broker == "webull",
                                                                  BrokerAccount.environment == "test").order_by(BrokerAccount.created_at.desc()))
            if account is None:
                print(json.dumps({"status": "NOT_CONFIGURED", "message": "No Webull Thailand TEST account is persisted"}, indent=2))
                return
            account_id = account.id
        config = WebullTestConfig.from_env()
        broker = WebullTestAdapter(config, resolver=PersistentOrderResolver(engine, account_id))
        asyncio.run(broker.attest_account())
        async def reconcile_now():
            with Session(engine) as session:
                account = session.get(BrokerAccount, account_id)
                result = await BrokerExecutionService(session, broker, account).reconcile(
                    history_start=datetime.now(timezone.utc) - timedelta(days=365),
                    history_end=datetime.now(timezone.utc))
                return [{"kind": item.kind.value, "object_type": item.object_type,
                         "object_id": item.object_id, "local": item.local, "broker": item.broker}
                        for item in result]
        result = asyncio.run(reconcile_now())
        print(json.dumps({"status": "MATCH" if all(x["kind"] == "MATCH" for x in result) else "MISMATCH",
                          "backend": "webull-th-test", "items": result}, indent=2)); return
    if args.command == "init-db":
        config = Config("alembic.ini")
        config.attributes["database_url"] = settings.database_url
        command.upgrade(config, "head")
        print("Database migrated")
    elif args.command == "seed-demo-data":
        seed(Path(args.csv))
        print(args.csv)
    else:
        if args.command in {"run-paper", "paper-run"}:
            print(run_paper(engine, settings, MomentumStrategy(), CSVProvider(args.csv).historical(args.symbol), args.quantity,
                            run_id=args.run_id, max_bars=args.max_bars))
            return
        with Session(engine) as session:
            run = session.get(Run, args.run_id) if args.run_id else session.scalar(select(Run).order_by(Run.created_at.desc()))
            if not run:
                parser.error("No matching run; run the paper simulator first")
            if args.command == "positions":
                result = [{"symbol": p.symbol, "quantity": p.quantity, "average_entry": p.average_entry,
                           "mark": p.mark, "unrealized_pnl": p.unrealized_pnl} for p in session.scalars(select(PositionRow).where(PositionRow.run_id == run.id))]
            elif args.command == "orders":
                result = [{"id": o.id, "signal_id": o.signal_id, "status": o.status, "payload": o.payload}
                          for o in session.scalars(select(OrderRow).join(SignalRow).where(SignalRow.run_id == run.id))]
            elif args.command == "fills":
                result = [{"id": f.id, "order_id": f.order_id, "trade_id": f.trade_id,
                           "quantity": f.quantity, "price": f.price, "fee": f.fee}
                          for f in session.scalars(select(FillRow).join(Trade).where(Trade.run_id == run.id))]
            elif args.command == "trades":
                result = [{"id": t.id, "version_id": t.version_id, "status": t.status, **t.payload} for t in session.scalars(select(Trade).where(Trade.run_id == run.id))]
            else:
                metrics = list(session.scalars(select(TradeMetrics).join(Trade).where(Trade.run_id == run.id)))
                result = {"run_id": run.id, "status": run.status, "cash": run.cash, "equity": run.equity,
                          "closed_trades": len(metrics), "net_pnl": sum(m.payload["net_pnl"] for m in metrics)}
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
