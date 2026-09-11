from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal, Protocol
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class OrderState(StrEnum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


TERMINAL_STATES = frozenset({OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED})


LEGAL_TRANSITIONS = {
    # Direct fill transitions recover a broker event that outran local submit persistence.
    OrderState.CREATED: {OrderState.SUBMITTING, OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.UNKNOWN},
    OrderState.SUBMITTING: {OrderState.SUBMITTED, OrderState.ACKNOWLEDGED, OrderState.REJECTED, OrderState.UNKNOWN},
    OrderState.SUBMITTED: {OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.REJECTED, OrderState.CANCEL_PENDING, OrderState.UNKNOWN},
    OrderState.ACKNOWLEDGED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCEL_PENDING, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED, OrderState.UNKNOWN},
    OrderState.PARTIALLY_FILLED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCEL_PENDING, OrderState.CANCELLED, OrderState.EXPIRED, OrderState.UNKNOWN},
    OrderState.CANCEL_PENDING: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELLED, OrderState.UNKNOWN},
    OrderState.UNKNOWN: {OrderState.SUBMITTED, OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCEL_PENDING, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED},
}


def money(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("Decimal must be finite")
    return result


class ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InstrumentRules(ExactModel):
    price_increment: Decimal | None = None
    quantity_increment: Decimal | None = None

    @field_validator("price_increment", "quantity_increment", mode="before")
    @classmethod
    def exact_optional(cls, value):
        return None if value is None else money(value)

    def validate(self, *, price: Decimal | None, quantity: Decimal) -> None:
        if self.quantity_increment is not None and quantity % self.quantity_increment:
            raise ValueError("Quantity does not match instrument increment")
        if price is not None and self.price_increment is not None and price % self.price_increment:
            raise ValueError("Price does not match instrument increment")


class BrokerOrderIntent(ExactModel):
    internal_order_id: str
    client_order_id: str
    run_id: str
    signal_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT"]
    quantity: Decimal
    limit_price: Decimal | None = None
    quote_timestamp: AwareDatetime
    submitted_at: AwareDatetime
    quote_age_limit_seconds: Decimal | None = None

    @field_validator("quantity", "limit_price", "quote_age_limit_seconds", mode="before")
    @classmethod
    def exact(cls, value):
        return None if value is None else money(value)

    @model_validator(mode="after")
    def validate_intent(self):
        if self.quantity <= 0 or (self.order_type == "LIMIT" and self.limit_price is None):
            raise ValueError("Invalid order intent")
        if self.quote_age_limit_seconds is not None:
            age = Decimal(str((self.submitted_at - self.quote_timestamp).total_seconds()))
            if age < 0 or age > self.quote_age_limit_seconds:
                from app.brokers.errors import StaleMarketData
                raise StaleMarketData("Quote is outside the explicit freshness limit")
        return self


class BrokerFill(ExactModel):
    execution_id: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    timestamp: AwareDatetime

    @field_validator("quantity", "price", "fee", mode="before")
    @classmethod
    def exact(cls, value):
        return money(value)

    @model_validator(mode="after")
    def validate_values(self):
        if self.quantity <= 0 or self.price <= 0 or self.fee < 0:
            raise ValueError("Invalid fill")
        return self


class BrokerEvent(ExactModel):
    event_id: str
    internal_order_id: str
    broker_order_id: str | None = None
    state: OrderState
    timestamp: AwareDatetime
    source: str
    reason: str | None = None
    fill: BrokerFill | None = None


class BrokerOrderView(ExactModel):
    internal_order_id: str
    client_order_id: str
    broker_order_id: str | None
    state: OrderState
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    filled_quantity: Decimal

    @field_validator("quantity", "filled_quantity", mode="before")
    @classmethod
    def exact(cls, value):
        return money(value)


class AccountState(ExactModel):
    account_ref: str
    environment: str
    cash: Decimal
    positions: dict[str, Decimal]
    timestamp: AwareDatetime

    @field_validator("cash", mode="before")
    @classmethod
    def exact_cash(cls, value):
        return money(value)

    @field_validator("positions", mode="before")
    @classmethod
    def exact_positions(cls, value):
        return {key: money(quantity) for key, quantity in value.items()}


class Broker(Protocol):
    async def submit_order(self, intent: BrokerOrderIntent) -> BrokerEvent: ...
    async def cancel_order(self, internal_order_id: str) -> BrokerEvent: ...
    async def query_order(self, internal_order_id: str) -> BrokerOrderView | None: ...
    async def query_open_orders(self) -> list[BrokerOrderView]: ...
    async def query_fills(self) -> list[BrokerEvent]: ...
    async def query_positions(self) -> dict[str, Decimal]: ...
    async def query_account_state(self) -> AccountState: ...
    async def recover(self) -> list[BrokerEvent]: ...
