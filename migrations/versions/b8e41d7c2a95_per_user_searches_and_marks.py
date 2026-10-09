"""Give each user their own search, notifications and marks on listings

The search and notification settings were shared; every user gets a copy of them, and of
the shared marks (status, notes) and matches of every listing. The night hours stay shared,
as the checking schedule. Each source keeps what it was last read with as a fetch scope.

Revision ID: b8e41d7c2a95
Revises: ed6bb6cf70ad
Create Date: 2026-10-09 16:00:00.000000

"""

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e41d7c2a95"
down_revision: str | Sequence[str] | None = "ed6bb6cf70ad"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# What SearchCriteria() was when nothing had been saved, and the order of PropertyType
DEFAULT_CRITERIA: dict[str, Any] = {
    "city": "ראשון לציון",
    "rooms_min": 4.0,
    "rooms_max": 4.0,
    "price_min": None,
    "price_max": None,
    "property_types": ["apartment", "garden", "duplex", "penthouse"],
    "private_only": False,
}
PROPERTY_TYPES = ["apartment", "garden", "duplex", "penthouse", "house", "unit", "studio", "other"]
PREFERENCE_KEYS = (
    "notifications_paused",
    "notify_price_drops",
    "quiet_hours_start",
    "quiet_hours_end",
)
SCHEDULE_KEYS = ("night_start", "night_end")

settings_table = sa.table(
    "settings",
    sa.column("key", sa.String),
    sa.column("value", sa.JSON),
    sa.column("updated_at", sa.DateTime),
)
users_table = sa.table(
    "users",
    sa.column("id", sa.Integer),
    sa.column("criteria", sa.JSON),
    sa.column("preferences", sa.JSON),
)
state_table = sa.table(
    "source_state",
    sa.column("source", sa.String),
    sa.column("custom_url", sa.Text),
    sa.column("fetch_signature", sa.String),
    sa.column("fetch_scope", sa.JSON),
)


def _now() -> datetime:
    # Stored as naive UTC, like UTCDateTime does
    return datetime.now(UTC).replace(tzinfo=None)


def _old_signature(criteria: dict[str, Any], custom_url: str | None) -> str:
    """runner.fetch_signature as it was."""
    keys = ("city", "rooms_min", "rooms_max", "price_min", "price_max", "property_types")
    payload = json.dumps(
        {"criteria": {key: criteria[key] for key in keys}, "custom_url": custom_url or None},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _scope(criteria: dict[str, Any], custom_url: str | None) -> dict[str, Any]:
    """searchers.FetchScope for one user's search, as the runner makes it."""
    if custom_url:
        return {"custom_url": custom_url, "searches": []}
    chosen = set(criteria["property_types"])
    search = {
        "city": criteria["city"],
        "rooms_min": float(criteria["rooms_min"]),
        "rooms_max": float(criteria["rooms_max"]),
        "price_min": criteria["price_min"],
        "price_max": criteria["price_max"],
        "property_types": [kind for kind in PROPERTY_TYPES if kind in chosen],
        "private_only": bool(criteria.get("private_only", False)),
    }
    return {"custom_url": None, "searches": [search]}


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("criteria", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("preferences", sa.JSON(), nullable=True))
    with op.batch_alter_table("source_state", schema=None) as batch_op:
        batch_op.add_column(sa.Column("fetch_scope", sa.JSON(), nullable=True))

    op.create_table(
        "listing_marks",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("matches", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_listing_marks_listing_id_listings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_listing_marks_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "listing_id", name=op.f("pk_listing_marks")),
    )
    with op.batch_alter_table("listing_marks", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_listing_marks_listing_id"), ["listing_id"], unique=False
        )

    connection = op.get_bind()
    saved = dict(connection.execute(sa.select(settings_table.c.key, settings_table.c.value)).all())
    criteria = saved.get("criteria")
    preferences = saved.get("preferences") or {}

    # Everyone starts from the search and notifications that were shared until now
    user_preferences = {key: preferences[key] for key in PREFERENCE_KEYS if key in preferences}
    connection.execute(
        users_table.update().values(
            criteria=sa.null() if criteria is None else criteria,
            preferences=user_preferences or sa.null(),
        )
    )
    if schedule := {key: preferences[key] for key in SCHEDULE_KEYS if key in preferences}:
        connection.execute(
            settings_table.insert().values(key="schedule", value=schedule, updated_at=_now())
        )
    shared = settings_table.c.key.in_(["criteria", "preferences"])
    connection.execute(settings_table.delete().where(shared))

    # The marks were shared, so each user gets them all
    connection.execute(
        sa.text(
            "INSERT INTO listing_marks (user_id, listing_id, matches, status, notes) "
            "SELECT users.id, listings.id, listings.matches, listings.status, listings.notes "
            "FROM users CROSS JOIN listings"
        )
    )

    # A source last read with the current search keeps it; any other (an older search) is read
    # as if for the first time, as the old signature check would have
    full_criteria = DEFAULT_CRITERIA | (criteria or {})
    for source, custom_url, signature in connection.execute(
        sa.select(state_table.c.source, state_table.c.custom_url, state_table.c.fetch_signature)
    ).all():
        if signature and signature == _old_signature(full_criteria, custom_url):
            connection.execute(
                state_table.update()
                .where(state_table.c.source == source)
                .values(fetch_scope=_scope(full_criteria, custom_url))
            )

    with op.batch_alter_table("source_state", schema=None) as batch_op:
        batch_op.drop_column("fetch_signature")
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_listings_status"))
        batch_op.drop_column("notes")
        batch_op.drop_column("status")
        batch_op.drop_column("matches")


def downgrade() -> None:
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("matches", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.add_column(
            sa.Column("status", sa.String(length=16), nullable=False, server_default="seen")
        )
        batch_op.add_column(sa.Column("notes", sa.Text(), nullable=True))
        batch_op.create_index(batch_op.f("ix_listings_status"), ["status"], unique=False)
    with op.batch_alter_table("source_state", schema=None) as batch_op:
        batch_op.add_column(sa.Column("fetch_signature", sa.String(length=64), nullable=True))

    # Back to shared settings and marks: the first user's
    connection = op.get_bind()
    first = connection.execute(
        sa.select(users_table.c.id, users_table.c.criteria, users_table.c.preferences)
        .order_by(users_table.c.id)
        .limit(1)
    ).first()
    if first is not None:
        connection.execute(
            sa.text(
                "UPDATE listings SET "
                "matches = (SELECT matches FROM listing_marks m "
                "WHERE m.listing_id = listings.id AND m.user_id = :user), "
                "status = (SELECT status FROM listing_marks m "
                "WHERE m.listing_id = listings.id AND m.user_id = :user), "
                "notes = (SELECT notes FROM listing_marks m "
                "WHERE m.listing_id = listings.id AND m.user_id = :user) "
                "WHERE EXISTS (SELECT 1 FROM listing_marks m "
                "WHERE m.listing_id = listings.id AND m.user_id = :user)"
            ),
            {"user": first.id},
        )
        schedule = dict(
            connection.execute(
                sa.select(settings_table.c.value).where(settings_table.c.key == "schedule")
            ).scalar()
            or {}
        )
        preferences = (first.preferences or {}) | schedule
        if first.criteria:
            row = {"key": "criteria", "value": first.criteria, "updated_at": _now()}
            connection.execute(settings_table.insert().values(**row))
        if preferences:
            row = {"key": "preferences", "value": preferences, "updated_at": _now()}
            connection.execute(settings_table.insert().values(**row))
    connection.execute(settings_table.delete().where(settings_table.c.key == "schedule"))

    with op.batch_alter_table("listing_marks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_listing_marks_listing_id"))
    op.drop_table("listing_marks")
    with op.batch_alter_table("source_state", schema=None) as batch_op:
        batch_op.drop_column("fetch_scope")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("preferences")
        batch_op.drop_column("criteria")
