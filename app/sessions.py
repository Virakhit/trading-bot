from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo


class SessionCalendar:
    def __init__(self, timezone_name="Asia/Bangkok", start="09:30", end="16:00", *, exchange=None):
        self.zone = ZoneInfo(timezone_name)
        self.start, self.end = time.fromisoformat(start), time.fromisoformat(end)
        self.exchange_name = exchange
        self.exchange = None
        if exchange:
            import exchange_calendars as xcals
            self.exchange = xcals.get_calendar(exchange)

    def state(self, timestamp: datetime) -> str:
        if timestamp.tzinfo is None:
            raise ValueError("Session timestamps must be timezone-aware")
        if self.exchange is not None:
            utc = timestamp.astimezone(timezone.utc)
            local_date = timestamp.astimezone(self.zone).date().isoformat()
            try:
                session = self.exchange.date_to_session(local_date, direction="none")
                row = self.exchange.schedule.loc[session]
            except (KeyError, ValueError):
                return "CLOSED"
            market_open, market_close = row["open"].to_pydatetime(), row["close"].to_pydatetime()
            if utc < market_open:
                return "PRE_MARKET"
            if utc >= market_close:
                return "AFTER_HOURS"
            return "OPEN"
        local = timestamp.astimezone(self.zone)
        if local.weekday() >= 5: return "CLOSED"
        if local.time() < self.start: return "PRE_MARKET"
        if local.time() <= self.end: return "OPEN"
        return "AFTER_HOURS"

    def allows_orders(self, timestamp: datetime) -> bool:
        return self.state(timestamp) == "OPEN"
