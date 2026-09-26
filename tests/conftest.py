import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from roomfinder import models  # noqa: F401 - registers the tables
from roomfinder.db import Base, create_sessionmaker


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
