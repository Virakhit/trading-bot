from pathlib import Path
from datetime import datetime
from typing import Protocol
import pandas as pd
from app.core.types import Bar


class MarketDataProvider(Protocol):
    def historical(self, symbol: str) -> list[Bar]: ...
    def quote(self, symbol: str, timestamp: datetime) -> Bar: ...


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
