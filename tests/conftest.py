import contextlib

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from roomfinder import (
    auth,
    models,  # noqa: F401 - registers the tables
)
from roomfinder.config import Settings
from roomfinder.db import Base, create_sessionmaker, utcnow
from roomfinder.main import create_app
from roomfinder.models import Listing, ListingMark, ListingStatus, Role, User

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """Tests must never talk to the real bot configured in the developer's .env."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    # Real Argon2 settings are slow on purpose; tests only need the same behavior
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=1024))


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


# --- the whole app -------------------------------------------------------------------


@pytest.fixture
def db(database_path):
    """A plain session on the app's database, for setting up and checking data."""
    with Session(create_engine(f"sqlite:///{database_path}")) as session:
        yield session


def add_listing(
    db: Session,
    source_id: str,
    *,
    status: str = ListingStatus.SEEN,
    matches: bool = True,
    notes: str | None = None,
    **overrides,
) -> Listing:
    """A stored listing, with the same mark (status, match, notes) for every user so far."""
    now = utcnow()
    fields = {
        "source": "yad2",
        "source_id": source_id,
        "url": f"https://www.yad2.co.il/realestate/item/{source_id}",
        "price": 6000,
        "rooms": 4.0,
        "sqm": 95,
        "floor": 2,
        "street": "הרצל",
        "house_number": "12",
        "neighborhood": "רמז",
        "city": "ראשון לציון",
        "property_type": "apartment",
        "is_agency": False,
        "images": [],
        "features": ["חניה"],
        "first_seen_at": now,
        "last_seen_at": now,
    } | overrides
    listing = Listing(**fields)
    db.add(listing)
    db.flush()
    for user in db.scalars(select(User)):
        db.add(
            ListingMark(
                user_id=user.id, listing_id=listing.id, matches=matches, status=status, notes=notes
            )
        )
    db.commit()
    return listing


def mark_of(db: Session, listing: Listing, user: User | int = 1) -> ListingMark | None:
    """A user's mark on a listing, as stored now."""
    db.expire_all()
    user_id = user if isinstance(user, int) else user.id
    return db.get(ListingMark, (user_id, listing.id))


def add_user(
    db: Session,
    username: str = "admin",
    role: Role = Role.ADMIN,
    active: bool = True,
    password: str = PASSWORD,
) -> User:
    user = User(
        username=username,
        username_key=username.casefold(),
        password_hash=auth.hash_password(password),
        role=role,
        is_active=active,
    )
    db.add(user)
    db.commit()
    return user


def sign_in(client: TestClient, username: str = "admin", password: str = PASSWORD) -> None:
    response = client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303, response.text


@pytest.fixture
def start_dashboard(database_path):
    """Starts the dashboard with some settings changed, e.g. as hosted behind a proxy."""
    with contextlib.ExitStack() as stack:

        def start(base_url: str = "http://testserver", **overrides) -> TestClient:
            settings = Settings(
                **{
                    "database_url": f"sqlite+aiosqlite:///{database_path}",
                    "scheduler_enabled": False,
                    "cdp_url": "http://127.0.0.1:1",
                    "allowed_hosts": ["testserver", "localhost"],
                }
                | overrides
            )
            return stack.enter_context(TestClient(create_app(settings), base_url=base_url))

        yield start


@pytest.fixture
def anonymous(start_dashboard):
    """The dashboard, for a visitor who isn't signed in."""
    return start_dashboard()


@pytest.fixture
def admin(db) -> User:
    return add_user(db)


@pytest.fixture
def client(anonymous, admin):
    """The dashboard, signed in as an admin."""
    sign_in(anonymous)
    return anonymous
