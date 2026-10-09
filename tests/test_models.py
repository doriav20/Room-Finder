from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import StatementError

from roomfinder.models import Listing


async def test_listing_round_trip_returns_aware_utc_datetimes(session):
    israel = timezone(timedelta(hours=3))
    posted = datetime(2026, 9, 1, 12, 0, tzinfo=israel)
    session.add(
        Listing(source="yad2", source_id="abc", url="https://example.com", posted_at=posted)
    )
    await session.commit()
    session.expire_all()

    listing = (await session.execute(select(Listing))).scalar_one()

    assert listing.posted_at == posted
    assert listing.posted_at.tzinfo == UTC
    assert listing.first_seen_at.tzinfo == UTC
    assert listing.images == []


async def test_naive_datetimes_are_rejected(session):
    session.add(
        Listing(source="yad2", source_id="x", url="https://x", posted_at=datetime(2026, 1, 1))
    )
    with pytest.raises(StatementError):
        await session.commit()
