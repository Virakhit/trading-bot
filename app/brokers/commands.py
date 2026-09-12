from datetime import datetime, timezone
from enum import StrEnum
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from app.brokers.errors import (BrokerCommandOutcomeUnknown, BrokerDefinitelyNotSent,
                                InvalidOrderTransition, OrderRejected, UnsafeEnvironmentError)
from app.brokers.models import Broker, BrokerEvent, BrokerOrderIntent, OrderState, TERMINAL_STATES
from app.brokers.service import BrokerExecutionService
from app.database.models import BrokerAccount, BrokerCommand, BrokerOrder
from app.journal import digest


class CommandType(StrEnum):
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"


class CommandStatus(StrEnum):
    PREPARED = "PREPARED"
    SENDING = "SENDING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    UNKNOWN = "UNKNOWN"
    FAILED_PRE_SEND = "FAILED_PRE_SEND"
    RECONCILED = "RECONCILED"
    REJECTED = "REJECTED"


UNRESOLVED = {CommandStatus.PREPARED, CommandStatus.SENDING, CommandStatus.UNKNOWN}


class DurableBrokerExecutor:
    """Commits broker intent before any network operation and resolves it afterwards."""

    def __init__(self, engine: Engine, broker: Broker, account_id: str):
        self.engine = engine
        self.broker = broker
        self.account_id = account_id

    def prepare_submit(self, order_id: str, *, allow_retry_after_pre_send: bool = False) -> str:
        with Session(self.engine) as session, session.begin():
            account, order = self._account_order(session, order_id)
            service = BrokerExecutionService(session, self.broker, account)
            current = OrderState(order.state)
            last = self._latest_command(session, order_id, CommandType.SUBMIT)
            if current == OrderState.CREATED:
                service.transition(order, OrderState.SUBMITTING)
            elif not (current == OrderState.SUBMITTING and allow_retry_after_pre_send and last
                      and last.status == CommandStatus.FAILED_PRE_SEND):
                if current == OrderState.SUBMITTING:
                    raise BrokerCommandOutcomeUnknown("Submission already durable; reconcile before retry")
                raise InvalidOrderTransition(f"Cannot prepare submit from {current}")
            self._ensure_no_unresolved(session, order_id, CommandType.SUBMIT)
            command = self._new_command(order, account, CommandType.SUBMIT)
            session.add(command)
            session.flush()
            return command.id

    def prepare_cancel(self, order_id: str, *, allow_retry_after_pre_send: bool = False) -> str:
        with Session(self.engine) as session, session.begin():
            account, order = self._account_order(session, order_id)
            current = OrderState(order.state)
            if current in TERMINAL_STATES:
                raise InvalidOrderTransition("Terminal order cannot be cancelled")
            last = self._latest_command(session, order_id, CommandType.CANCEL)
            service = BrokerExecutionService(session, self.broker, account)
            if current != OrderState.CANCEL_PENDING:
                service.transition(order, OrderState.CANCEL_PENDING)
            elif not (allow_retry_after_pre_send and last and last.status == CommandStatus.FAILED_PRE_SEND):
                raise BrokerCommandOutcomeUnknown("Cancellation already durable; reconcile before retry")
            self._ensure_no_unresolved(session, order_id, CommandType.CANCEL)
            command = self._new_command(order, account, CommandType.CANCEL)
            session.add(command)
            session.flush()
            return command.id

    async def dispatch(self, command_id: str) -> BrokerEvent:
        command_type, order_id, intent = self._mark_sending(command_id)
        try:
            if command_type == CommandType.SUBMIT:
                event = await self.broker.submit_order(intent)
            else:
                event = await self.broker.cancel_order(order_id)
        except (BrokerDefinitelyNotSent, UnsafeEnvironmentError) as exc:
            self._resolve_without_event(command_id, CommandStatus.FAILED_PRE_SEND, type(exc).__name__)
            raise
        except OrderRejected as exc:
            self._resolve_rejected(command_id, command_type, str(exc))
            raise
        except Exception as exc:
            self._resolve_without_event(command_id, CommandStatus.UNKNOWN, type(exc).__name__)
            raise BrokerCommandOutcomeUnknown("Broker command outcome is unknown; reconcile before retry") from exc
        self._persist_success(command_id, event)
        return event

    def recover_inflight(self) -> list[str]:
        """Convert crash-left SENDING commands to UNKNOWN; never resubmit them."""
        with Session(self.engine) as session, session.begin():
            commands = list(session.scalars(select(BrokerCommand).where(
                BrokerCommand.account_id == self.account_id,
                BrokerCommand.status == CommandStatus.SENDING)))
            for command in commands:
                command.status = CommandStatus.UNKNOWN
                command.safe_error_category = "PROCESS_INTERRUPTED_AFTER_SEND_BOUNDARY"
            return [command.id for command in commands]

    async def reconcile_unknown(self, command_id: str):
        with Session(self.engine) as session:
            command = session.get(BrokerCommand, command_id)
            if command is None or command.account_id != self.account_id:
                raise OrderRejected("Unknown broker command")
            if command.status not in {CommandStatus.UNKNOWN, CommandStatus.SENDING}:
                raise InvalidOrderTransition("Only ambiguous commands require reconciliation")
            order = session.get(BrokerOrder, command.order_id)
            self.broker.bind_order(order.id, order.client_order_id)
            client_order_id = command.client_order_id
        remote = await self.broker.query_order_by_client_id(client_order_id)
        if remote is None:
            return None
        fills = [event for event in await self.broker.query_fills()
                 if event.internal_order_id == order.id or event.internal_order_id == client_order_id]
        with Session(self.engine) as session, session.begin():
            command = session.get(BrokerCommand, command_id)
            order = session.get(BrokerOrder, command.order_id)
            service = BrokerExecutionService(session, self.broker, session.get(BrokerAccount, command.account_id))
            for event in fills:
                if event.internal_order_id != order.id:
                    event = event.model_copy(update={"internal_order_id": order.id})
                service.apply_event(event)
            if remote.broker_order_id and order.broker_order_id is None:
                order.broker_order_id = remote.broker_order_id
            if remote.state == OrderState.UNKNOWN:
                command.safe_error_category = "BROKER_STATUS_UNKNOWN"
                return remote
            # A terminal fill state without executions is not sufficient evidence for accounting.
            if remote.state in {OrderState.PARTIALLY_FILLED, OrderState.FILLED}:
                if order.filled_quantity < remote.filled_quantity:
                    command.safe_error_category = "BROKER_FILLED_QUANTITY_NOT_RECONCILED"
                    return remote
                if remote.state == OrderState.FILLED and order.filled_quantity != order.quantity:
                    command.safe_error_category = "BROKER_FILLED_WITHOUT_EXECUTIONS"
                    return remote
            if OrderState(order.state) != remote.state:
                service.transition(order, remote.state)
            command.status = CommandStatus.RECONCILED
            command.resolved_at = datetime.now(timezone.utc)
            command.correlation_id = remote.broker_order_id or remote.client_order_id
            command.safe_error_category = None
        return remote

    def get_command(self, command_id: str) -> BrokerCommand:
        with Session(self.engine) as session:
            command = session.get(BrokerCommand, command_id)
            if command is None or command.account_id != self.account_id:
                raise OrderRejected("Unknown broker command")
            session.expunge(command)
            return command

    def _mark_sending(self, command_id: str):
        with Session(self.engine) as session, session.begin():
            command = session.get(BrokerCommand, command_id)
            if command is None or command.account_id != self.account_id:
                raise OrderRejected("Unknown broker command")
            if command.status in {CommandStatus.SENDING, CommandStatus.UNKNOWN}:
                raise BrokerCommandOutcomeUnknown("Command may already have reached broker")
            if command.status != CommandStatus.PREPARED:
                raise InvalidOrderTransition(f"Cannot dispatch command from {command.status}")
            order = session.get(BrokerOrder, command.order_id)
            self.broker.bind_order(order.id, order.client_order_id)
            command.status = CommandStatus.SENDING
            command.sent_at = datetime.now(timezone.utc)
            intent = BrokerOrderIntent.model_validate(order.payload)
            return CommandType(command.command_type), order.id, intent

    def _persist_success(self, command_id: str, event: BrokerEvent) -> None:
        with Session(self.engine) as session, session.begin():
            command = session.get(BrokerCommand, command_id)
            account = session.get(BrokerAccount, command.account_id)
            service = BrokerExecutionService(session, self.broker, account)
            service.apply_event(event)
            command.status = CommandStatus.ACKNOWLEDGED
            command.resolved_at = datetime.now(timezone.utc)
            command.correlation_id = event.event_id
            command.safe_error_category = None

    def _resolve_without_event(self, command_id: str, status: CommandStatus, category: str) -> None:
        with Session(self.engine) as session, session.begin():
            command = session.get(BrokerCommand, command_id)
            command.status = status
            command.resolved_at = datetime.now(timezone.utc) if status != CommandStatus.UNKNOWN else None
            command.safe_error_category = category

    def _resolve_rejected(self, command_id: str, command_type: CommandType, reason: str) -> None:
        with Session(self.engine) as session, session.begin():
            command = session.get(BrokerCommand, command_id)
            order = session.get(BrokerOrder, command.order_id)
            command.status = CommandStatus.REJECTED
            command.resolved_at = datetime.now(timezone.utc)
            command.safe_error_category = "BROKER_REJECTED"
            service = BrokerExecutionService(session, self.broker, session.get(BrokerAccount, command.account_id))
            if command_type == CommandType.SUBMIT:
                event = BrokerEvent(event_id=f"command:{command.id}:rejected", internal_order_id=order.id,
                                    broker_order_id=order.broker_order_id, state=OrderState.REJECTED,
                                    timestamp=command.resolved_at, source="broker-command", reason=reason[:300])
                service.apply_event(event)
            else:
                service.transition(order, OrderState.UNKNOWN)

    @staticmethod
    def _new_command(order: BrokerOrder, account: BrokerAccount, command_type: CommandType) -> BrokerCommand:
        payload = {"order_id": order.id, "client_order_id": order.client_order_id,
                   "command_type": command_type, "intent_hash": digest(order.payload)}
        return BrokerCommand(order_id=order.id, account_id=account.id, command_type=command_type,
                             client_order_id=order.client_order_id, payload_hash=digest(payload),
                             status=CommandStatus.PREPARED)

    @staticmethod
    def _latest_command(session: Session, order_id: str, command_type: CommandType):
        return session.scalar(select(BrokerCommand).where(BrokerCommand.order_id == order_id,
                              BrokerCommand.command_type == command_type).order_by(BrokerCommand.created_at.desc()))

    @staticmethod
    def _ensure_no_unresolved(session: Session, order_id: str, command_type: CommandType) -> None:
        pending = session.scalar(select(BrokerCommand.id).where(
            BrokerCommand.order_id == order_id,
            BrokerCommand.command_type == command_type,
            BrokerCommand.status.in_(tuple(UNRESOLVED))))
        if pending:
            raise BrokerCommandOutcomeUnknown("Unresolved broker command exists; reconcile before retry")

    def _account_order(self, session: Session, order_id: str):
        account = session.get(BrokerAccount, self.account_id)
        order = session.get(BrokerOrder, order_id)
        if account is None or order is None or order.account_id != self.account_id:
            raise OrderRejected("Unknown account/order")
        return account, order
