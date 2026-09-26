"""User-editable settings, stored as JSON rows in the settings table."""

from datetime import time

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from roomfinder.models import Setting
from roomfinder.schemas import SearchCriteria

CRITERIA_KEY = "criteria"
PREFERENCES_KEY = "preferences"


class Preferences(BaseModel):
    notifications_paused: bool = False
    notify_price_drops: bool = True
    # Telegram messages arrive without sound during these hours
    quiet_hours_start: time = time(23, 0)
    quiet_hours_end: time = time(7, 0)
    # Sources are checked less often at night, when few listings are posted
    night_start: time = time(0, 30)
    night_end: time = time(6, 30)


async def _load[T: BaseModel](session: AsyncSession, key: str, model: type[T]) -> T:
    row = await session.get(Setting, key)
    return model() if row is None else model.model_validate(row.value)


async def _save(session: AsyncSession, key: str, value: BaseModel) -> None:
    data = value.model_dump(mode="json")
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=data))
    else:
        row.value = data


async def get_criteria(session: AsyncSession) -> SearchCriteria:
    return await _load(session, CRITERIA_KEY, SearchCriteria)


async def save_criteria(session: AsyncSession, criteria: SearchCriteria) -> None:
    await _save(session, CRITERIA_KEY, criteria)


async def get_preferences(session: AsyncSession) -> Preferences:
    return await _load(session, PREFERENCES_KEY, Preferences)


async def save_preferences(session: AsyncSession, preferences: Preferences) -> None:
    await _save(session, PREFERENCES_KEY, preferences)
