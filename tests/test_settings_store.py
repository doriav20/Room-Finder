from datetime import time

import pytest

from roomfinder.schemas import SearchCriteria
from roomfinder.settings_store import (
    Preferences,
    get_criteria,
    get_preferences,
    save_criteria,
    save_preferences,
)
from roomfinder.timeutils import in_daily_window


async def test_defaults_are_used_until_something_is_saved(session):
    assert await get_criteria(session) == SearchCriteria()
    assert await get_preferences(session) == Preferences()


async def test_saved_settings_round_trip_and_can_be_updated(session):
    await save_criteria(session, SearchCriteria(rooms_max=4.5, price_max=7000))
    await save_preferences(session, Preferences(quiet_hours_start=time(22, 0)))
    await session.commit()

    await save_criteria(session, SearchCriteria(price_max=8000))
    await session.commit()
    session.expire_all()

    assert (await get_criteria(session)).price_max == 8000
    assert (await get_preferences(session)).quiet_hours_start == time(22, 0)


@pytest.mark.parametrize(
    ("moment", "start", "end", "expected"),
    [
        (time(23, 30), time(23), time(7), True),
        (time(3, 0), time(23), time(7), True),
        (time(7, 0), time(23), time(7), False),
        (time(12, 0), time(23), time(7), False),
        (time(12, 0), time(9), time(17), True),
        (time(12, 0), time(9), time(9), False),
    ],
)
def test_in_daily_window_handles_windows_across_midnight(moment, start, end, expected):
    assert in_daily_window(moment, start, end) is expected
