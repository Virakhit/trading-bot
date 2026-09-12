import json
import logging
from collections import Counter


class SafeMetrics:
    def __init__(self): self.counts = Counter()
    def inc(self, name, amount=1): self.counts[name] += amount
    def snapshot(self): return dict(self.counts)


class JsonLogFormatter(logging.Formatter):
    SAFE = {"run_id", "signal_id", "order_id", "client_order_id", "command_id", "broker_order_id", "symbol", "state", "event_type"}
    def format(self, record):
        data = {"level": record.levelname, "message": record.getMessage()}
        data.update({k: v for k, v in getattr(record, "context", {}).items() if k in self.SAFE})
        return json.dumps(data, sort_keys=True)
