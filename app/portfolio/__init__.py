from dataclasses import dataclass, field
from datetime import datetime
from app.core.types import Fill


@dataclass
class Position:
    quantity: int = 0
    average_entry: float = 0
    mark: float = 0

    @property
    def unrealized_pnl(self) -> float:
        return self.quantity * (self.mark - self.average_entry)


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0
    fees: float = 0
    trades_today: int = 0
    day_start_equity: float | None = None
    current_day: object = None
    consecutive_losses: int = 0
    cooldown_until: datetime | None = None

    @property
    def equity(self) -> float:
        return self.cash + sum(p.quantity * p.mark for p in self.positions.values())

    @property
    def buying_power(self) -> float:
        return self.cash

    def new_day(self, timestamp: datetime) -> None:
        if self.current_day != timestamp.date():
            self.current_day = timestamp.date()
            self.day_start_equity = self.equity
            self.trades_today = 0

    def apply(self, symbol: str, side: str, fill: Fill) -> float:
        position = self.positions.setdefault(symbol, Position())
        gross = 0.0
        if side == "BUY":
            cost = fill.quantity * fill.price + fill.fee
            if cost > self.cash + 1e-9:
                raise ValueError("INSUFFICIENT_CASH")
            position.average_entry = (position.average_entry * position.quantity + fill.quantity * fill.price) / (position.quantity + fill.quantity)
            position.quantity += fill.quantity
            self.cash -= cost
        elif side == "SELL":
            if fill.quantity > position.quantity:
                raise ValueError("NO_SHORT_SELLING")
            gross = fill.quantity * (fill.price - position.average_entry)
            position.quantity -= fill.quantity
            self.cash += fill.quantity * fill.price - fill.fee
            self.realized_pnl += gross
            if position.quantity == 0:
                position.average_entry = 0
        else:
            raise ValueError("INVALID_SIDE")
        position.mark = fill.price
        self.fees += fill.fee
        return gross
