import argparse
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
from app.database.models import PositionRow, Trade, TradeMetrics, Run
from app.market_data import CSVProvider
from app.strategies import MomentumStrategy


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
    parser.add_argument("command", choices=["init-db", "seed-demo-data", "run-paper", "positions", "trades", "performance"])
    parser.add_argument("--csv", default="data/demo.csv")
    parser.add_argument("--symbol", default="DEMO")
    parser.add_argument("--quantity", type=int, default=10)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    settings = Settings()
    Path("data").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(filename="logs/events.jsonl", level=logging.INFO, format="%(message)s")
    if args.command == "init-db":
        config = Config("alembic.ini")
        config.attributes["database_url"] = settings.database_url
        command.upgrade(config, "head")
        print("Database migrated")
    elif args.command == "seed-demo-data":
        seed(Path(args.csv))
        print(args.csv)
    else:
        engine = engine_for(settings.database_url)
        if args.command == "run-paper":
            print(run_paper(engine, settings, MomentumStrategy(), CSVProvider(args.csv).historical(args.symbol), args.quantity))
            return
        with Session(engine) as session:
            run = session.get(Run, args.run_id) if args.run_id else session.scalar(select(Run).order_by(Run.created_at.desc()))
            if not run:
                parser.error("No matching run; run the paper simulator first")
            if args.command == "positions":
                result = [{"symbol": p.symbol, "quantity": p.quantity, "average_entry": p.average_entry,
                           "mark": p.mark, "unrealized_pnl": p.unrealized_pnl} for p in session.scalars(select(PositionRow).where(PositionRow.run_id == run.id))]
            elif args.command == "trades":
                result = [{"id": t.id, "version_id": t.version_id, "status": t.status, **t.payload} for t in session.scalars(select(Trade).where(Trade.run_id == run.id))]
            else:
                metrics = list(session.scalars(select(TradeMetrics).join(Trade).where(Trade.run_id == run.id)))
                result = {"run_id": run.id, "status": run.status, "cash": run.cash, "equity": run.equity,
                          "closed_trades": len(metrics), "net_pnl": sum(m.payload["net_pnl"] for m in metrics)}
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
