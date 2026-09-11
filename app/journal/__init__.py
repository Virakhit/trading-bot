import hashlib
import inspect
import json
import logging
import subprocess
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database.models import StrategyRow, StrategyVersion, SystemEvent
from app.strategies import Strategy

logger = logging.getLogger("paper")


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def register_strategy(session: Session, strategy: Strategy) -> StrategyVersion:
    config = strategy.config()
    config_hash = digest(config)
    code_hash = digest(inspect.getsource(type(strategy)))
    existing = session.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.strategy_id, StrategyVersion.version == strategy.version))
    if existing:
        if existing.config_hash != config_hash or existing.code_hash != code_hash:
            raise ValueError("Strategy changed: increment version before running")
        return existing
    if session.get(StrategyRow, strategy.strategy_id) is None:
        session.add(StrategyRow(id=strategy.strategy_id, name=strategy.name))
        session.flush()
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
        git_sha = result.stdout.strip() if result.returncode == 0 else None
    except FileNotFoundError:
        git_sha = None
    version = StrategyVersion(strategy_id=strategy.strategy_id, version=strategy.version, config=config,
                              config_hash=config_hash, code_hash=code_hash, source_code=inspect.getsource(type(strategy)), git_sha=git_sha)
    session.add(version)
    session.flush()
    return version


def event(session: Session, run_id: str, timestamp: datetime, kind: str, signal_id: str | None = None, **payload: object) -> None:
    session.add(SystemEvent(run_id=run_id, timestamp=timestamp, event_type=kind, signal_id=signal_id, payload=payload))
    logger.info(json.dumps({"event": kind, "run_id": run_id, "timestamp": timestamp.isoformat(), "signal_id": signal_id, **payload}))
