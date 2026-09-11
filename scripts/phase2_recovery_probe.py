"""Read-only probe proving broker lifecycle visibility across processes."""
import argparse
import json
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.database import engine_for
from app.database.models import BrokerAccount, BrokerEventRow, BrokerFillRow, BrokerOrder, BrokerPosition


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--order-id", required=True)
    args = parser.parse_args()
    with Session(engine_for(args.database_url)) as session:
        order = session.get(BrokerOrder, args.order_id)
        if order is None:
            parser.error("Order not found")
        account = session.get(BrokerAccount, order.account_id)
        position = session.scalar(select(BrokerPosition).where(BrokerPosition.account_id == account.id,
                                                                 BrokerPosition.symbol == order.symbol))
        result = {"order_id": order.id, "client_order_id": order.client_order_id,
                  "broker_order_id": order.broker_order_id, "state": order.state,
                  "filled_quantity": str(order.filled_quantity), "cash": str(account.cash),
                  "position_quantity": str(position.quantity if position else 0),
                  "events": session.scalar(select(func.count()).select_from(BrokerEventRow).where(BrokerEventRow.order_id == order.id)),
                  "fills": session.scalar(select(func.count()).select_from(BrokerFillRow).where(BrokerFillRow.order_id == order.id))}
    print(json.dumps(result, sort_keys=True))
