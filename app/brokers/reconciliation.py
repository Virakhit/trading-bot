from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.brokers.models import AccountState, BrokerEvent, BrokerOrderView
from app.database.models import BrokerAccount, BrokerFillRow, BrokerOrder, BrokerPosition


class Difference(StrEnum):
    MATCH = "MATCH"
    LOCAL_MISSING = "LOCAL_MISSING"
    BROKER_MISSING = "BROKER_MISSING"
    STATUS_MISMATCH = "STATUS_MISMATCH"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    CASH_MISMATCH = "CASH_MISMATCH"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    PAYLOAD_MISMATCH = "PAYLOAD_MISMATCH"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ReconciliationItem:
    kind: Difference
    object_type: str
    object_id: str
    local: str | None
    broker: str | None


def reconcile(session: Session, account: BrokerAccount, broker_orders: list[BrokerOrderView],
              broker_account: AccountState, broker_fills: list[BrokerEvent] | None = None) -> list[ReconciliationItem]:
    """Detection only: never mutates or deletes history."""
    result: list[ReconciliationItem] = []
    local_orders = {o.id: o for o in session.scalars(select(BrokerOrder).where(BrokerOrder.account_id == account.id))}
    grouped_remote: dict[str, list[BrokerOrderView]] = {}
    for order in broker_orders:
        grouped_remote.setdefault(order.internal_order_id, []).append(order)
    remote_orders: dict[str, BrokerOrderView] = {}
    for order_id, candidates in grouped_remote.items():
        first = candidates[0]
        if any(candidate != first for candidate in candidates[1:]):
            result.append(ReconciliationItem(Difference.UNKNOWN, "order_duplicate", order_id, None,
                                             f"{len(candidates)} conflicting remote records"))
        remote_orders[order_id] = first
    for order_id in sorted(local_orders.keys() | remote_orders.keys()):
        local, remote = local_orders.get(order_id), remote_orders.get(order_id)
        if local is None:
            result.append(ReconciliationItem(Difference.LOCAL_MISSING, "order", order_id, None, remote.state))
        elif remote is None:
            result.append(ReconciliationItem(Difference.BROKER_MISSING, "order", order_id, local.state, None))
        elif local.state != remote.state:
            result.append(ReconciliationItem(Difference.STATUS_MISMATCH, "order", order_id, local.state, remote.state))
        elif local.filled_quantity != remote.filled_quantity:
            result.append(ReconciliationItem(Difference.QUANTITY_MISMATCH, "order", order_id, str(local.filled_quantity), str(remote.filled_quantity)))
        else:
            result.append(ReconciliationItem(Difference.MATCH, "order", order_id, local.state, remote.state))
    local_fills = {f.execution_id: f for f in session.scalars(select(BrokerFillRow).join(BrokerOrder).where(BrokerOrder.account_id == account.id))}
    remote_fills = {event.fill.execution_id: (event, event.fill) for event in (broker_fills or []) if event.fill}
    for execution_id in sorted(local_fills | remote_fills):
        if execution_id not in local_fills:
            result.append(ReconciliationItem(Difference.LOCAL_MISSING, "fill", execution_id, None, "present"))
        elif execution_id not in remote_fills:
            result.append(ReconciliationItem(Difference.BROKER_MISSING, "fill", execution_id, "present", None))
        else:
            local, (event, remote) = local_fills[execution_id], remote_fills[execution_id]
            same = (local.order_id == event.internal_order_id and local.quantity == remote.quantity and
                    local.price == remote.price and local.fee == remote.fee)
            result.append(ReconciliationItem(Difference.MATCH if same else Difference.PAYLOAD_MISMATCH, "fill",
                                             execution_id, f"{local.order_id}:{local.quantity}:{local.price}:{local.fee}",
                                             f"{event.internal_order_id}:{remote.quantity}:{remote.price}:{remote.fee}"))
    if account.account_ref != broker_account.account_ref or account.environment != broker_account.environment:
        result.append(ReconciliationItem(Difference.UNKNOWN, "account", account.id,
                                         f"{account.environment}:{account.account_ref}",
                                         f"{broker_account.environment}:{broker_account.account_ref}"))
        return result
    if account.cash != broker_account.cash:
        result.append(ReconciliationItem(Difference.CASH_MISMATCH, "account", account.id, str(account.cash), str(broker_account.cash)))
    local_positions = {p.symbol: p.quantity for p in session.scalars(select(BrokerPosition).where(BrokerPosition.account_id == account.id))}
    for symbol in sorted(local_positions.keys() | broker_account.positions.keys()):
        local, remote = local_positions.get(symbol, Decimal(0)), broker_account.positions.get(symbol, Decimal(0))
        result.append(ReconciliationItem(Difference.MATCH if local == remote else Difference.POSITION_MISMATCH,
                                         "position", symbol, str(local), str(remote)))
    return result
