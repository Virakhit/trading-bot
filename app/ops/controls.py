from datetime import datetime, timezone
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from app.database.models import OperationalControl


class ControlStore:
    def __init__(self, engine): self.engine = engine

    def get(self):
        try:
            with Session(self.engine) as session:
                row = session.get(OperationalControl, "global")
                return row.state if row else "RUNNING"
        except OperationalError:
            # A pre-0007 local database is safe to read as RUNNING; writes still
            # require the normal migration command and will fail closed.
            return "RUNNING"

    def set(self, state: str, reason: str | None = None):
        if state not in {"RUNNING", "PAUSED", "KILL_SWITCH"}: raise ValueError("Invalid operational control")
        with Session(self.engine) as session, session.begin():
            row = session.get(OperationalControl, "global")
            if row is None: row = OperationalControl(key="global", state=state, reason=reason); session.add(row)
            else: row.state, row.reason, row.updated_at = state, reason, datetime.now(timezone.utc)
        return state

    def permits_new_orders(self): return self.get() == "RUNNING"
