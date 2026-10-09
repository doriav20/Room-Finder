from datetime import time

import pytest

from roomfinder.models import User
from roomfinder.schemas import SearchCriteria
from roomfinder.settings_store import (
    Preferences,
    Schedule,
    get_schedule,
    save_schedule,
    set_user_criteria,
    set_user_preferences,
    user_criteria,
    user_preferences,
)
from roomfinder.timeutils import in_daily_window


def make_user(username: str) -> User:
    return User(username=username, username_key=username, password_hash="x", role="member")


async def test_defaults_are_used_until_something_is_saved(session):
    user = make_user("noa")
    session.add(user)
    await session.commit()

    assert user_criteria(user) == SearchCriteria()
    assert user_preferences(user) == Preferences()
    assert await get_schedule(session) == Schedule()


async def test_each_user_keeps_their_own_settings(session):
    noa, dan = make_user("noa"), make_user("dan")
    set_user_criteria(noa, SearchCriteria(rooms_max=4.5, price_max=7000))
    set_user_preferences(noa, Preferences(quiet_hours_start=time(22, 0)))
    set_user_criteria(dan, SearchCriteria(rooms_min=5, rooms_max=5.5))
    session.add_all([noa, dan])
    await session.commit()
    ids = noa.id, dan.id
    session.expire_all()

    noa, dan = [await session.get(User, user_id) for user_id in ids]
    assert (user_criteria(noa).price_max, user_criteria(noa).rooms_max) == (7000, 4.5)
    assert user_preferences(noa).quiet_hours_start == time(22, 0)
    assert user_criteria(dan).rooms_min == 5
    assert user_preferences(dan) == Preferences()


async def test_the_schedule_is_shared_and_can_be_updated(session):
    await save_schedule(session, Schedule(night_start=time(1, 0)))
    await session.commit()
    await save_schedule(session, Schedule(night_end=time(5, 0)))
    await session.commit()
    session.expire_all()

    assert await get_schedule(session) == Schedule(night_end=time(5, 0))


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
