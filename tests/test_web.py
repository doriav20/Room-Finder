import json
import re
from datetime import timedelta

import pytest
from markupsafe import escape

from conftest import PASSWORD, add_listing, add_user, mark_of, sign_in
from roomfinder.db import utcnow
from roomfinder.models import ListingStatus, PriceChange, Role, Setting, User
from roomfinder.schemas import SearchCriteria
from roomfinder.sources import build_sources


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
    assert mark_of(db, listing).notes == "להתקשר בערב"


def test_mark_all_new_as_seen(client, db):
    listing = add_listing(db, "a", status=ListingStatus.NEW)

    response = client.post("/listings/mark-seen", follow_redirects=False)

    assert response.status_code == 303
    assert mark_of(db, listing).status == ListingStatus.SEEN


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


def test_text_search(client, db):
    add_listing(db, "a", price=5100, description="מרפסת שמש <גדולה> וחנייה בטאבו")
    add_listing(db, "b", price=5200, street="ביאליק", description="לשותפים, יש מרפסת")
    add_listing(db, "c", price=5300, street="הגפן", title="דירה, הגפן 1")

    page = client.get("/?tab=all&q=מרפסת+-שותפים").text
    nothing = client.get("/?tab=all&q=ג'קוזי").text
    by_street = client.get("/?tab=all&q=הגפן").text

    assert "5,100 ₪" in page and "5,200 ₪" not in page and "5,300 ₪" not in page
    assert "<mark>מרפסת</mark> שמש &lt;גדולה&gt;" in page
    assert '<details class="small mt-2 listing-description" open>' in page
    assert 'value="מרפסת -שותפים"' in page
    assert 'href="?tab=all" title="ניקוי החיפוש"' in page
    # The tab counts follow the search
    assert 'הכול <span class="badge text-bg-light">1</span>' in page
    # A listing without an ad text is searched by its title and address only
    assert "ל-1 מודעות בלשונית הזו עוד אין טקסט מודעה" in page
    assert "5,300 ₪" in by_street and "📍 <mark>הגפן</mark> 12" in by_street
    assert "אין מודעות שמתאימות לחיפוש" in nothing


def test_text_search_carries_over_to_more_cards(client, db):
    for number in range(80):
        add_listing(db, f"x{number}", description="מרפסת" if number < 60 else "חצר")

    page = client.get("/?tab=all&q=מרפסת").text
    more = client.get("/listings/cards?tab=all&q=מרפסת&offset=48").text

    assert page.count("<mark>מרפסת</mark>") == 48
    assert "q=%D7%9E%D7%A8%D7%A4%D7%A1%D7%AA&offset=48" in page
    assert more.count("<mark>מרפסת</mark>") == 12 and "טען עוד" not in more


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


def test_filtering_by_several_sites(client, db):
    add_listing(db, "a", price=5100)
    add_listing(db, "b", price=5200, source="madlan")
    add_listing(db, "c", price=5300, source="komo")
    original = add_listing(db, "d", price=5400, source="homeless")
    add_listing(db, "d-copy", source="onmap", duplicate_of_id=original.id)

    page = client.get("/?tab=all&source=yad2&source=madlan").text
    everything = client.get("/?tab=all").text
    by_copy = client.get("/?tab=all&source=onmap").text

    assert "5,100 ₪" in page and "5,200 ₪" in page
    assert "5,300 ₪" not in page and "5,400 ₪" not in page
    assert re.search(r'value="yad2"\s+checked', page)
    assert not re.search(r'value="komo"\s+checked', page)
    assert "יד2, מדלן" in page and "כל האתרים" in everything
    # The pages that follow keep both sites
    assert "source=yad2&amp;source=madlan" in page
    # A listing counts as on a site when one of its copies is there
    assert "5,400 ₪" in by_copy and "5,100 ₪" not in by_copy


def test_furniture_and_parking_criteria_are_saved(client, db):
    response = client.post(
        "/settings/criteria",
        data={"city": "ראשון לציון", "rooms_min": "4", "rooms_max": "4", "furnished": "yes",
              "parking_required": "1"},
        follow_redirects=False,
    )  # fmt: skip

    assert response.status_code == 303
    db.expire_all()
    saved = db.get(User, 1).criteria
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
    assert 'data-link="https://www.yad2.co.il/realestate/rent"' in saved.text
    bad = client.post("/sources/yad2/settings", data={"interval_minutes": "1"})
    assert bad.status_code == 400


def test_sources_page_links_to_each_search_with_the_filters(client):
    page = client.get("/sources").text

    for source in build_sources().values():
        assert f'data-link="{escape(source.search_url(SearchCriteria()))}"' in page


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
    assert mark_of(db, listing).matches is True
    page = client.get("/settings?saved=criteria").text
    assert "החיפוש שלך נשמר" in page
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
    assert mark_of(db, inside).matches is True
    assert mark_of(db, outside).matches is False
    saved = db.get(User, 1).criteria
    assert (saved["areas"], saved["unknown_area_ok"]) == (["רמז"], True)
    assert "השכונות נשמרו" in client.get("/areas?saved=1").text
    assert "📍 שכונת רמז" in client.get("/settings").text
    assert client.post("/areas", data={"areas": ["לא שכונה"]}).status_code == 400


def test_saving_the_search_keeps_the_neighborhoods_of_the_same_city(client, db):
    client.post("/areas", data={"areas": ["רמז", "נווה ים"]})
    search = {"city": "ראשון לציון", "rooms_min": "4", "rooms_max": "4"}

    client.post("/settings/criteria", data=search)
    db.expire_all()
    assert db.get(User, 1).criteria["areas"] == ["נווה ים", "רמז"]

    # Only cities every site can be searched in
    assert client.post("/settings/criteria", data=search | {"city": "חולון"}).status_code == 400


def test_listings_filter_by_official_neighborhood(client, db):
    add_listing(db, "a", area="רמז", price=5100)
    add_listing(db, "b", neighborhood='רמב"ם', area="אברמוביץ'", price=5200, street="ביאליק")

    page = client.get("/?tab=all").text
    only_ramaz = client.get("/?tab=all&area=רמז").text

    assert '<option value="אברמוביץ&#39;">' in page
    assert "(לפי המפה: אברמוביץ&#39;)" in page  # placed by its map point, not the site's name
    assert "5,100 ₪" in only_ramaz and "5,200 ₪" not in only_ramaz


def test_map_view_shows_the_tab_on_a_map(client, db):
    add_listing(db, "a", price=5100)

    page = client.get("/?tab=all&view=map&price_max=7000").text

    assert 'id="listings-map"' in page and "listings_map.js" in page
    assert "listing-card" not in page
    assert 'data-src="/api/listings/map?tab=all&amp;price_max=7000&amp;view=map"' in page
    # Switching tabs, or back to the cards, keeps the filters
    assert 'href="?tab=new&amp;price_max=7000&amp;view=map"' in page
    assert 'href="?tab=all&amp;price_max=7000"' in page


def test_map_places_listings_by_their_point_a_copy_or_their_neighborhood(client, db):
    add_listing(db, "point", latitude=31.97, longitude=34.79, price=5100, street="א")
    original = add_listing(db, "no-point", price=5200, street="ב")
    add_listing(
        db, "copy", source="madlan", duplicate_of_id=original.id, latitude=31.96, longitude=34.8
    )
    add_listing(db, "area", area="רמז", price=5300, street="ג")
    add_listing(db, "nowhere", neighborhood="מרכז", price=5400, street="ד")
    add_listing(db, "expensive", latitude=31.95, longitude=34.78, price=9000, street="ה")

    data = client.get("/api/listings/map?tab=all&price_max=8000").json()

    by_price = {item["price"]: item for item in data["listings"]}
    assert set(by_price) == {5100, 5200, 5300}
    assert (by_price[5100]["lat"], by_price[5100]["approximate"]) == (31.97, False)
    assert (by_price[5200]["lat"], by_price[5200]["lng"]) == (31.96, 34.8)
    assert by_price[5300]["approximate"] is True
    assert data["without_location"] == 1
    assert data["bounds"] is not None and data["areas"] == []


def test_map_outlines_the_chosen_neighborhoods(client, db):
    client.post("/areas", data={"areas": ["רמז"], "unknown_area_ok": "1"})

    data = client.get("/api/listings/map?tab=all").json()

    assert [area["name"] for area in data["areas"]] == ["רמז"]


def test_cards_show_every_photo(client, db):
    photos = [f"https://img.yad2.co.il/Pic/{index}.jpeg" for index in range(3)]
    add_listing(db, "many", images=photos, price=5100)
    add_listing(db, "one", images=photos[:1], price=5200, street="ביאליק")

    page = client.get("/?tab=all").text

    assert all(f'src="{photo}"' in page for photo in photos)
    assert page.count("gallery-next") == 1  # only the card with several photos
    assert "<span data-gallery-total>3</span>" in page


# --- each user's own search and marks ----------------------------------------------


@pytest.fixture
def member(db) -> User:
    return add_user(db, "noa", role=Role.MEMBER)


def test_each_user_sees_listings_through_their_own_search(client, db, member):
    small = add_listing(db, "small", price=5100, rooms=4.0)
    big = add_listing(db, "big", price=5200, rooms=5.0, street="ביאליק")
    mark_of(db, small, member).matches = False
    db.commit()
    mark_of(db, big, 1).matches = False
    db.commit()

    admin_page = client.get("/?tab=all").text
    sign_in(client, "noa", PASSWORD)
    member_page = client.get("/?tab=all").text

    assert "5,100 ₪" in admin_page and "5,200 ₪" not in admin_page
    assert "5,200 ₪" in member_page and "5,100 ₪" not in member_page


def test_marks_and_notes_are_each_users_own(client, db, member):
    listing = add_listing(db, "a", status=ListingStatus.NEW)

    client.post(f"/listings/{listing.id}/status", data={"status": "hidden"})
    client.post(f"/listings/{listing.id}/notes", data={"notes": "לא בשבילי"})
    client.post("/listings/mark-seen")

    assert (mark_of(db, listing, 1).status, mark_of(db, listing, 1).notes) == (
        "hidden",
        "לא בשבילי",
    )
    theirs = mark_of(db, listing, member)
    assert (theirs.status, theirs.notes) == (ListingStatus.NEW, None)
    sign_in(client, "noa", PASSWORD)
    page = client.get("/?tab=new").text
    assert "6,000 ₪" in page and "לא בשבילי" not in page


def test_saving_a_search_changes_only_that_users_matches(client, db, member):
    listing = add_listing(db, "a", rooms=5.0, matches=False)
    sign_in(client, "noa", PASSWORD)

    client.post(
        "/settings/criteria",
        data={"city": "ראשון לציון", "rooms_min": "5", "rooms_max": "5.5"},
    )

    assert mark_of(db, listing, member).matches is True
    assert mark_of(db, listing, 1).matches is False
    db.expire_all()
    assert db.get(User, member.id).criteria["rooms_max"] == 5.5
    assert db.get(User, 1).criteria is None


def test_notification_settings_are_each_users_own(client, db, member):
    client.post("/settings/preferences", data={"notifications_paused": "1"})

    assert "התראות מושהות" in client.get("/settings").text
    sign_in(client, "noa", PASSWORD)
    assert "התראות מושהות" not in client.get("/settings").text


def test_members_see_the_sources_but_only_admins_control_them(client, db, member):
    admin_page = client.get("/sources").text
    sign_in(client, "noa", PASSWORD)
    member_page = client.get("/sources").text

    run_button = 'hx-post="/sources/yad2/run"'
    assert run_button in admin_page and "החיפוש המשולב" in admin_page
    assert run_button not in member_page and "החיפוש המשולב" not in member_page
    assert "כתובת חיפוש מותאמת" not in member_page
    assert "יד2" in member_page
    for action in ("run", "toggle", "settings"):
        assert client.post(f"/sources/yad2/{action}", data={}).status_code == 403


def test_the_combined_search_covers_every_active_user(client, db, member):
    client.post(
        "/settings/criteria", data={"city": "ראשון לציון", "rooms_min": "4", "rooms_max": "5"}
    )
    sign_in(client, "noa", PASSWORD)
    client.post(
        "/settings/criteria",
        data={"city": "ראשון לציון", "rooms_min": "5", "rooms_max": "5.5", "price_max": "8000"},
    )
    sign_in(client, "admin", PASSWORD)

    page = client.get("/sources").text

    assert "ראשון לציון · 4-5.5 חדרים · כל המחירים" in page
    assert "2 פעילים" in page


def test_only_admins_set_the_night_hours(client, db, member):
    response = client.post(
        "/settings/schedule", data={"night_start": "01:00", "night_end": "05:00"},
        follow_redirects=False,
    )  # fmt: skip
    assert response.status_code == 303
    assert 'value="01:00"' in client.get("/settings").text

    sign_in(client, "noa", PASSWORD)
    assert "שעות לילה" not in client.get("/settings").text
    assert client.post("/settings/schedule", data={"night_start": "02:00"}).status_code == 403
    db.expire_all()
    assert db.get(Setting, "schedule").value["night_start"] == "01:00:00"


def test_neighborhoods_are_each_users_own(client, db, member):
    client.post("/areas", data={"areas": ["רמז"], "unknown_area_ok": "1"})
    chosen = re.compile(r'value="רמז" id="area-\d+"\s+checked')

    assert chosen.search(client.get("/areas").text)
    sign_in(client, "noa", PASSWORD)
    assert not chosen.search(client.get("/areas").text)
    db.expire_all()
    assert db.get(User, 1).criteria["areas"] == ["רמז"]
    assert db.get(User, member.id).criteria is None
