from dataclasses import dataclass
from typing import Protocol
import numpy as np
from app.core.types import Bar, Signal


@dataclass(frozen=True)
class Context:
    bars: tuple[Bar, ...]
    position_quantity: int


class Strategy(Protocol):
    strategy_id: str
    version: str
    name: str
    required_timeframe: str
    warmup_bars: int
    def config(self) -> dict: ...
    def evaluate(self, context: Context) -> Signal: ...


class MomentumStrategy:
    strategy_id = "demo-momentum"
    version = "1.0.0"
    name = "Three-bar momentum"
    required_timeframe = "1m"
    warmup_bars = 3

    def config(self) -> dict:
        return {"warmup_bars": self.warmup_bars, "timeframe": self.required_timeframe}

    def evaluate(self, context: Context) -> Signal:
        bar = context.bars[-1]
        mean = float(np.mean([b.close for b in context.bars[-self.warmup_bars:]]))
        action = "HOLD"
        if len(context.bars) >= self.warmup_bars:
            if bar.close > mean and not context.position_quantity:
                action = "BUY"
            elif bar.close < mean and context.position_quantity:
                action = "EXIT"
        return Signal(strategy_id=self.strategy_id, strategy_version=self.version,
                      symbol=bar.symbol, timestamp=bar.timestamp, action=action,
                      signal_price=bar.close, reasons=[f"MOMENTUM_{action}"], indicators={"sma": mean})
