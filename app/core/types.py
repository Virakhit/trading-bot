from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, field_validator, model_validator


def uid() -> str:
    return str(uuid4())


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    @field_validator("timestamp", check_fields=False)
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class Bar(Model):
    symbol: str = Field(min_length=1)
    timestamp: AwareDatetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    volume: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_prices(self) -> "Bar":
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.bid > self.ask:
            raise ValueError("Invalid OHLC or crossed quote")
        return self

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class Signal(Model):
    signal_id: str = Field(default_factory=uid)
    strategy_id: str
    strategy_version: str
    symbol: str
    timestamp: AwareDatetime
    action: Literal["BUY", "SELL", "EXIT", "HOLD"]
    signal_price: float = Field(gt=0)
    reasons: list[str]
    indicators: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class OrderRequest(Model):
    order_id: str = Field(default_factory=uid)
    signal_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_price: float | None = Field(None, gt=0)

    @model_validator(mode="after")
    def valid_limit(self) -> "OrderRequest":
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("Limit price required")
        return self


class Fill(Model):
    fill_id: str = Field(default_factory=uid)
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)
    fee: float = Field(ge=0)
    timestamp: AwareDatetime


class ExecutionResult(Model):
    status: Literal["FILLED", "PARTIALLY_FILLED", "CANCELLED", "REJECTED"]
    fills: list[Fill] = Field(default_factory=list)
    reason: str | None = None
