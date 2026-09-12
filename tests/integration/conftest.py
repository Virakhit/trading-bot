from alembic import command
from alembic.config import Config
import pytest

from app.database import engine_for


@pytest.fixture
def engine(tmp_path):
    url = "sqlite:///" + str(tmp_path / "webull-test.db")
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    db = engine_for(url)
    yield db
    db.dispose()
