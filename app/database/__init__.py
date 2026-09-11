from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session


def engine_for(url: str) -> Engine:
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
    return engine
