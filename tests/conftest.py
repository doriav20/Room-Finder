import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from roomfinder import models  # noqa: F401 - registers the tables
from roomfinder.db import Base, create_sessionmaker


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """Tests must never talk to the real bot configured in the developer's .env."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")


@pytest.fixture
def database_path(tmp_path):
    """A SQLite file with the schema, for tests that start the whole app."""
    path = tmp_path / "test.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{path}"))
    return path


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(engine):
    return create_sessionmaker(engine)


@pytest.fixture
async def session(sessionmaker):
    async with sessionmaker() as session:
        yield session
