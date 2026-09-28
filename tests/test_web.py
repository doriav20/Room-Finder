import json
import re
from datetime import timedelta

from conftest import add_listing
from roomfinder.db import utcnow
from roomfinder.models import Listing, ListingStatus, PriceChange, Setting


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


def test_csv_export_neutralizes_formulas_from_ads(client, db):
    add_listing(db, "a", description='=HYPERLINK("http://evil.example","לחץ")', notes="+1")

    text = client.get("/export.csv").text

    assert "'=HYPERLINK" in text and ",'+1," in text


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
    assert "הבוט לא מוגדר" in client.get("/account").text
    assert client.post("/account/telegram/link").status_code == 503


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
        headers={"Origin": "http://testserver"},
    )
    assert same_site.status_code == 200
    # A page from another port of the same machine is another site too
    other_port = client.post(
        f"/listings/{listing.id}/status",
        data={"status": "seen"},
        headers={"Origin": "http://testserver:3000"},
    )
    assert other_port.status_code == 403


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


def test_neighborhoods_page_shows_the_map_and_what_is_in_each(client, db):
    add_listing(db, "a", area="רמז")
    add_listing(db, "b", area="רמז", street="ביאליק")
    add_listing(db, "c", area=None, neighborhood="מרכז", street="הגפן")

    page = client.get("/areas").text

    assert "vendor/leaflet/leaflet.js" in page
    embedded = re.search(
        r'<script type="application/json" id="areas-data">(.*?)</script>', page, re.S
    )
    counts = {area["name"]: area["count"] for area in json.loads(embedded.group(1))["areas"]}
    assert len(counts) == 40 and counts["רמז"] == 2 and counts["נווה ים"] == 0
    assert "(1 עכשיו)" in page  # the one that can't be placed


def test_choosing_neighborhoods_saves_them_and_judges_listings_again(client, db):
    inside = add_listing(db, "a", neighborhood="רמז", area="רמז")
    outside = add_listing(db, "b", neighborhood="נווה ים", area="נווה ים", street="ביאליק")

    response = client.post(
        "/areas", data={"areas": ["רמז"], "unknown_area_ok": "1"}, follow_redirects=False
    )

    assert response.status_code == 303
    db.expire_all()
    assert db.get(Listing, inside.id).matches is True
    assert db.get(Listing, outside.id).matches is False
    saved = db.get(Setting, "criteria").value
    assert (saved["areas"], saved["unknown_area_ok"]) == (["רמז"], True)
    assert "השכונות נשמרו" in client.get("/areas?saved=1").text
    assert "📍 שכונת רמז" in client.get("/settings").text
    assert client.post("/areas", data={"areas": ["לא שכונה"]}).status_code == 400


def test_saving_the_search_keeps_the_neighborhoods_of_the_same_city(client, db):
    client.post("/areas", data={"areas": ["רמז", "נווה ים"]})
    search = {"city": "ראשון לציון", "rooms_min": "4", "rooms_max": "4"}

    client.post("/settings/criteria", data=search)
    assert db.get(Setting, "criteria").value["areas"] == ["נווה ים", "רמז"]

    client.post("/settings/criteria", data=search | {"city": "חולון"})
    db.expire_all()
    assert db.get(Setting, "criteria").value["areas"] == []
    assert "אין עדיין מפת שכונות" in client.get("/areas").text


def test_listings_filter_by_official_neighborhood(client, db):
    add_listing(db, "a", area="רמז", price=5100)
    add_listing(db, "b", neighborhood='רמב"ם', area="אברמוביץ'", price=5200, street="ביאליק")

    page = client.get("/?tab=all").text
    only_ramaz = client.get("/?tab=all&area=רמז").text

    assert '<option value="אברמוביץ&#39;">' in page
    assert "(לפי המפה: אברמוביץ&#39;)" in page  # placed by its map point, not the site's name
    assert "5,100 ₪" in only_ramaz and "5,200 ₪" not in only_ramaz
