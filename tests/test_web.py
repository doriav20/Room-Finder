from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from roomfinder.config import Settings
from roomfinder.db import utcnow
from roomfinder.main import create_app
from roomfinder.models import Listing, ListingStatus, PriceChange, Setting


@pytest.fixture
def db(database_path):
    with Session(create_engine(f"sqlite:///{database_path}")) as session:
        yield session


@pytest.fixture
def client(database_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        scheduler_enabled=False,
        cdp_url="http://127.0.0.1:1",
        allowed_hosts=["testserver", "localhost"],
    )
    with TestClient(create_app(settings)) as client:
        yield client


def add_listing(db: Session, source_id: str, **overrides) -> Listing:
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
        "status": ListingStatus.SEEN,
        "matches": True,
        "images": [],
        "features": ["חניה"],
        "first_seen_at": now,
        "last_seen_at": now,
    } | overrides
    listing = Listing(**fields)
    db.add(listing)
    db.commit()
    return listing


def test_listings_page_opens_on_new_listings(client, db):
    add_listing(db, "old")
    add_listing(db, "fresh", status=ListingStatus.NEW, price=5800, street="ביאליק")

    page = client.get("/")

    assert page.status_code == 200
    assert "5,800 ₪" in page.text
    assert "6,000 ₪" not in page.text  # the "new" tab is open
    assert "6,000 ₪" in client.get("/?tab=all").text


def test_non_matching_and_duplicate_listings_are_hidden_by_default(client, db):
    original = add_listing(db, "a")
    add_listing(db, "copy", duplicate_of_id=original.id, source="madlan")
    add_listing(db, "big", rooms=5.0, matches=False, price=9900)

    page = client.get("/?tab=all").text

    assert "9,900 ₪" not in page
    assert "גם ב:" in page
    assert "9,900 ₪" in client.get("/?tab=all&all=1").text


def test_price_drop_badge(client, db):
    listing = add_listing(db, "a", price=6000)
    db.add(PriceChange(listing_id=listing.id, old_price=6500, new_price=6000, seen_at=utcnow()))
    db.commit()

    assert "ירד מ-6,500 ₪" in client.get("/?tab=all").text


def test_status_buttons_toggle(client, db):
    listing = add_listing(db, "a")

    first = client.post(f"/listings/{listing.id}/status", data={"status": "favorite"})
    second = client.post(f"/listings/{listing.id}/status", data={"status": "favorite"})

    assert "status-favorite" in first.text
    assert "status-seen" in second.text
    assert client.post(f"/listings/{listing.id}/status", data={"status": "x"}).status_code == 400


def test_notes_are_saved(client, db):
    listing = add_listing(db, "a")

    response = client.post(f"/listings/{listing.id}/notes", data={"notes": "להתקשר בערב"})

    assert response.status_code == 204
    db.expire_all()
    assert db.get(Listing, listing.id).notes == "להתקשר בערב"


def test_mark_all_new_as_seen(client, db):
    listing = add_listing(db, "a", status=ListingStatus.NEW)

    response = client.post("/listings/mark-seen", follow_redirects=False)

    assert response.status_code == 303
    db.expire_all()
    assert db.get(Listing, listing.id).status == ListingStatus.SEEN


def test_updates_api_reports_new_matching_listings(client, db):
    seen = add_listing(db, "a")
    new = add_listing(db, "b", status=ListingStatus.NEW, street="ביאליק")
    add_listing(db, "c", status=ListingStatus.NEW, matches=False)

    data = client.get(f"/api/updates?since_id={seen.id}").json()

    assert [item["id"] for item in data["listings"]] == [new.id]
    assert "ביאליק 12" in data["listings"][0]["summary"]
    assert data["latest_id"] >= new.id


def test_csv_export(client, db):
    add_listing(db, "a", notes="מעניין", furnished=True, parking=True, parking_spaces=2)

    response = client.get("/export.csv")

    assert response.headers["content-type"].startswith("text/csv")
    assert response.text.startswith("﻿אתר,")
    assert "יד2" in response.text and "מעניין" in response.text
    assert ",מרוהטת,2 חניות," in response.text


def test_furniture_and_parking_filters_and_chips(client, db):
    add_listing(db, "a", price=5100, furnished=True, parking=True, description="דירה\nמרווחת")
    add_listing(db, "b", price=5200, street="ביאליק", furnished=False, parking=False)
    add_listing(db, "c", price=5300, street="הגפן")

    page = client.get("/?tab=all").text
    furnished = client.get("/?tab=all&furnished=yes").text
    no_parking = client.get("/?tab=all&parking=no").text

    assert "🛋 מרוהטת" in page and "🛋 לא מרוהטת" in page and "🚗 אין חניה" in page
    assert "תיאור המודעה" in page
    assert "5,100 ₪" in furnished and "5,200 ₪" not in furnished and "5,300 ₪" not in furnished
    assert "5,200 ₪" in no_parking and "5,100 ₪" not in no_parking


def test_furniture_and_parking_criteria_are_saved(client, db):
    response = client.post(
        "/settings/criteria",
        data={"city": "ראשון לציון", "rooms_min": "4", "rooms_max": "4", "furnished": "yes",
              "parking_required": "1"},
        follow_redirects=False,
    )  # fmt: skip

    assert response.status_code == 303
    saved = db.get(Setting, "criteria").value
    assert (saved["furnished"], saved["parking_required"]) == ("yes", True)
    assert '<option value="yes" selected>מרוהטת בלבד</option>' in client.get("/settings").text
    bad = client.post("/settings/criteria", data={"rooms_min": "4", "furnished": "maybe"})
    assert bad.status_code == 400


def test_sources_page_and_source_settings(client):
    assert "יד2" in client.get("/sources").text

    toggled = client.post("/sources/yad2/toggle")
    saved = client.post(
        "/sources/yad2/settings",
        data={"interval_minutes": "15", "custom_url": "https://www.yad2.co.il/realestate/rent"},
    )

    assert "הפעל" in toggled.text  # the source is now off
    assert 'value="15"' in saved.text
    bad = client.post("/sources/yad2/settings", data={"interval_minutes": "1"})
    assert bad.status_code == 400


def test_unknown_sources_are_not_found(client):
    assert client.post("/sources/nope/toggle").status_code == 404
    assert client.post("/sources/nope/settings", data={}).status_code == 404
    assert client.get("/sources/nope/row").status_code == 404


def test_saving_criteria_recomputes_matches(client, db):
    listing = add_listing(db, "a", rooms=4.5, matches=False)

    response = client.post(
        "/settings/criteria",
        data={
            "city": "ראשון לציון",
            "rooms_min": "4",
            "rooms_max": "4.5",
            "price_max": "7000",
            "property_types": ["apartment", "garden"],
            "exclude_keywords": "סאבלט\n\n",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.expire_all()
    assert db.get(Listing, listing.id).matches is True
    page = client.get("/settings?saved=criteria").text
    assert "החיפוש נשמר" in page
    assert 'value="7000"' in page


def test_saving_preferences(client):
    response = client.post(
        "/settings/preferences",
        data={"notifications_paused": "1", "quiet_hours_start": "22:30", "quiet_hours_end": "bad"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    page = client.get("/settings").text
    assert 'value="22:30"' in page
    assert "התראות מושהות" in page


def test_telegram_actions_need_a_bot(client):
    assert "הבוט לא מוגדר" in client.get("/settings").text
    assert client.post("/telegram/link").status_code == 503


def test_cross_site_writes_are_blocked(client, db):
    listing = add_listing(db, "a")

    response = client.post(
        f"/listings/{listing.id}/status",
        data={"status": "hidden"},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    same_site = client.post(
        f"/listings/{listing.id}/status",
        data={"status": "hidden"},
        headers={"Origin": "http://localhost:8000"},
    )
    assert same_site.status_code == 200


def test_unknown_host_names_are_rejected(client):
    assert client.get("/", headers={"Host": "rebind.evil.example"}).status_code == 400


def test_removed_listings_have_their_own_tab(client, db):
    add_listing(db, "gone", removed_at=utcnow() - timedelta(hours=1), price=4321)

    assert "4,321 ₪" not in client.get("/?tab=all").text
    assert "4,321 ₪" in client.get("/?tab=removed").text


def test_loading_more_cards(client, db):
    for index in range(50):
        add_listing(db, f"l{index}", price=5000 + index, street=f"רחוב {index}")

    first_page = client.get("/?tab=all").text
    more = client.get("/listings/cards?tab=all&offset=48").text

    assert "טען עוד" in first_page
    assert more.count("listing-card") == 2
