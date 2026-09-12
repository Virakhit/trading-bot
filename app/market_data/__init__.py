from pathlib import Path
from datetime import datetime
from typing import Protocol
from dataclasses import dataclass, field
import math
import pandas as pd
from app.core.types import Bar


class MarketDataProvider(Protocol):
    def historical(self, symbol: str) -> list[Bar]: ...
    def quote(self, symbol: str, timestamp: datetime) -> Bar: ...


@dataclass(frozen=True)
class MarketSnapshot:
    bar: Bar
    source: str = "unknown"
    session: str | None = None
    freshness_seconds: float = 0.0
    quality: tuple[str, ...] = ()

    @property
    def symbol(self): return self.bar.symbol
    @property
    def timestamp(self): return self.bar.timestamp


def validate_bar(bar: Bar, *, previous: Bar | None = None) -> tuple[str, ...]:
    issues = []
    prices = tuple(float(getattr(bar, name)) for name in ("open", "high", "low", "close", "bid", "ask"))
    if not all(math.isfinite(value) for value in prices):
        issues.append("NON_FINITE_PRICE")
    elif any(value <= 0 for value in prices):
        issues.append("NON_POSITIVE_PRICE")
    if bar.high < bar.low or bar.bid > bar.ask:
        issues.append("INVALID_QUOTE_RANGE")
    if previous and bar.timestamp < previous.timestamp: issues.append("TIMESTAMP_REGRESSION")
    if previous and bar.timestamp == previous.timestamp: issues.append("DUPLICATE_TIMESTAMP")
    if not math.isfinite(float(bar.volume)) or bar.volume < 0: issues.append("INVALID_VOLUME")
    return tuple(issues)


class ReplayProvider:
    def __init__(self, bars: list[Bar], source: str = "replay", *, strict: bool = True):
        self.bars = list(bars)
        self.source = source
        issues = []
        previous = None
        for bar in self.bars:
            issues.extend(validate_bar(bar, previous=previous)); previous = bar
        self.quality_failures = tuple(sorted(set(issues)))
        if issues and strict: raise ValueError(f"Market data quality failure: {self.quality_failures}")

    def snapshots(self, symbol: str):
        return [MarketSnapshot(bar, self.source) for bar in self.historical(symbol)]

    def historical(self, symbol: str):
        return [bar for bar in self.bars if bar.symbol == symbol]

    def quote(self, symbol: str, timestamp: datetime):
        return next(bar for bar in self.historical(symbol) if bar.timestamp == timestamp)

    def quality_report(self):
        return {"source": self.source, "failures": self.quality_failures}

    def persist_quality(self, session):
        from app.database.models import DataQualityEvent
        now = self.bars[-1].timestamp if self.bars else datetime.now()
        for code in self.quality_failures:
            session.add(DataQualityEvent(symbol=self.bars[0].symbol if self.bars else "UNKNOWN",
                                         timestamp=now, source=self.source, code=code))


class SyntheticProvider(ReplayProvider):
    def __init__(self, bars: list[Bar]): super().__init__(bars, "synthetic")


class CSVProvider:
    def __init__(self, path: str | Path):
        frame = pd.read_csv(path)
        self.bars = [Bar.model_validate(row) for row in frame.to_dict("records")]
        keys = [(b.symbol, b.timestamp) for b in self.bars]
        if len(keys) != len(set(keys)) or keys != sorted(keys):
            raise ValueError("CSV must be unique and sorted by symbol, timestamp")

    def historical(self, symbol: str) -> list[Bar]:
        return [b for b in self.bars if b.symbol == symbol]

    def quote(self, symbol: str, timestamp: datetime) -> Bar:
        return next(b for b in self.bars if b.symbol == symbol and b.timestamp == timestamp)
