from datetime import datetime, time
from zoneinfo import ZoneInfo


class SessionCalendar:
    def __init__(self, timezone_name="Asia/Bangkok", start="09:30", end="16:00"):
        self.zone = ZoneInfo(timezone_name)
        self.start, self.end = time.fromisoformat(start), time.fromisoformat(end)

    def state(self, timestamp: datetime) -> str:
        if timestamp.tzinfo is None:
            raise ValueError("Session timestamps must be timezone-aware")
        local = timestamp.astimezone(self.zone)
        if local.weekday() >= 5: return "CLOSED"
        if local.time() < self.start: return "PRE_MARKET"
        if local.time() <= self.end: return "OPEN"
        return "AFTER_HOURS"

    def allows_orders(self, timestamp: datetime) -> bool:
        return self.state(timestamp) == "OPEN"
