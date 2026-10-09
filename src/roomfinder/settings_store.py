"""Settings: each user's own search and notifications, and the checking schedule for everyone.

A user's settings are JSON on their row (users.criteria, users.preferences); the schedule is
a JSON row in the settings table, and only admins change it.
"""

from datetime import time

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from roomfinder.models import Setting, User
from roomfinder.schemas import SearchCriteria

SCHEDULE_KEY = "schedule"


class Preferences(BaseModel):
    """One user's notifications."""

    notifications_paused: bool = False
    notify_price_drops: bool = True
    # Telegram messages arrive without sound during these hours
    quiet_hours_start: time = time(23, 0)
    quiet_hours_end: time = time(7, 0)


class Schedule(BaseModel):
    """When the sources are checked, for everyone."""

    # Sources are checked less often at night, when few listings are posted
    night_start: time = time(0, 30)
    night_end: time = time(6, 30)


def user_criteria(user: User) -> SearchCriteria:
    return SearchCriteria.model_validate(user.criteria or {})


def set_user_criteria(user: User, criteria: SearchCriteria) -> None:
    user.criteria = criteria.model_dump(mode="json")


def user_preferences(user: User) -> Preferences:
    return Preferences.model_validate(user.preferences or {})


def set_user_preferences(user: User, preferences: Preferences) -> None:
    user.preferences = preferences.model_dump(mode="json")


async def get_schedule(session: AsyncSession) -> Schedule:
    row = await session.get(Setting, SCHEDULE_KEY)
    return Schedule() if row is None else Schedule.model_validate(row.value)


async def save_schedule(session: AsyncSession, schedule: Schedule) -> None:
    data = schedule.model_dump(mode="json")
    row = await session.get(Setting, SCHEDULE_KEY)
    if row is None:
        session.add(Setting(key=SCHEDULE_KEY, value=data))
    else:
        row.value = data
