from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select
from telegram.error import BadRequest

from roomfinder.config import Settings
from roomfinder.models import Listing, ListingMark, ListingStatus, Notification, TelegramChat, User
from roomfinder.notify.telegram import (
    PairingCodes,
    TelegramService,
    format_new_listing,
    format_price_drop,
    listing_keyboard,
)

OWNER_ID = 1001
STRANGER_ID = 2002


def make_listing(**overrides) -> Listing:
    fields = {
        "id": 7,
        "source": "yad2",
        "source_id": "abc",
        "url": "https://www.yad2.co.il/realestate/item/abc",
        "price": 6200,
        "rooms": 4.0,
        "sqm": 95,
        "floor": 3,
        "street": "הרצל",
        "house_number": "12",
        "neighborhood": "רמז",
        "is_agency": False,
        "property_type": "apartment",
        "images": ["https://img.yad2.co.il/Pic/1.jpeg"],
        "features": ["חניה", 'ממ"ד'],
    } | overrides
    return Listing(**fields)


class FakeBot:
    def __init__(
        self, photo_error: Exception | None = None, album_error: Exception | None = None
    ) -> None:
        self.photo_error = photo_error
        self.album_error = album_error
        self.photos: list[dict] = []
        self.albums: list[dict] = []
        self.messages: list[dict] = []

    async def send_photo(self, chat_id, **kwargs):
        if self.photo_error:
            raise self.photo_error
        self.photos.append({"chat_id": chat_id} | kwargs)
        return SimpleNamespace(message_id=len(self.photos))

    async def send_media_group(self, chat_id, media, **kwargs):
        if self.album_error:
            raise self.album_error
        self.albums.append({"chat_id": chat_id, "media": media} | kwargs)

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append({"chat_id": chat_id, "text": text} | kwargs)
        return SimpleNamespace(message_id=100 + len(self.messages))


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


@pytest.fixture
def service(sessionmaker):
    settings = Settings(telegram_bot_token="123456:TEST", timezone="Asia/Jerusalem")
    service = TelegramService(settings, sessionmaker, {"yad2": "יד2"})
    service.bot = FakeBot()
    return service


async def add_account(session, username: str = "owner") -> User:
    account = User(username=username, username_key=username, password_hash="x", role="member")
    session.add(account)
    await session.commit()
    return account


@pytest.fixture
async def owner(session) -> User:
    """The dashboard account that the paired Telegram chat acts for."""
    return await add_account(session)


@pytest.fixture
async def paired(service, owner):
    code = service.pairing.create(owner.id)
    assert await service.pair(code, chat_id=OWNER_ID, user_id=OWNER_ID, name="Owner")
    return service


def start_update(user_id: int, chat_type: str = "private"):
    reply = Recorder()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=user_id, type=chat_type),
        effective_user=SimpleNamespace(id=user_id, full_name="Someone"),
        message=SimpleNamespace(reply_text=reply),
    )
    return update, reply


def test_pairing_codes_are_single_use_and_expire():
    codes = PairingCodes()
    code = codes.create(account_id=7)
    assert codes.consume(code).account_id == 7
    assert codes.consume(code) is None

    expired = PairingCodes(ttl=timedelta(seconds=-1))
    assert expired.consume(expired.create(7)) is None
    assert codes.consume("made-up") is None


async def test_chats_belong_to_accounts_and_stop_with_them(service, session):
    member = User(username="noa", username_key="noa", password_hash="x", role="member")
    session.add(member)
    await session.commit()

    assert await service.pair(service.pairing.create(member.id), 11, 11, "Noa")
    chat = await session.get(TelegramChat, 11)
    assert chat.account_id == member.id
    assert await service.is_allowed(11)

    member.is_active = False
    await session.commit()
    assert not await service.is_allowed(11)
    assert await service.allowed_chat_ids() == []


def test_new_listing_message():
    text = format_new_listing(make_listing(), "יד2")

    assert text.splitlines() == [
        "🏠 <b>דירה חדשה ביד2</b>",
        "<b>6,200 ₪</b>",
        "4 חד׳ · 95 מ״ר · קומה 3",
        "📍 הרצל 12, רמז",
        "🏷 פרטי · דירה",
        '✨ חניה · ממ"ד',
    ]


def test_message_shows_furniture_and_parking_when_known():
    furnished = make_listing(furnished=True, parking=True, parking_spaces=2)
    partial = make_listing(furnished=True, furniture_details="ריהוט חלקי", parking=False)

    assert "🛋 מרוהטת · 🚗 2 חניות" in format_new_listing(furnished, "יד2").splitlines()
    assert '✨ ממ"ד' in format_new_listing(furnished, "יד2").splitlines()
    assert "🛋 מרוהטת חלקית · 🚗 אין חניה" in format_new_listing(partial, "יד2").splitlines()


def test_message_names_the_neighborhood_on_the_map_when_the_site_says_otherwise():
    text = format_new_listing(
        make_listing(city="ראשון לציון", neighborhood='רמב"ם', area="אברמוביץ'"), "יד2"
    )
    same = format_new_listing(
        make_listing(city="ראשון לציון", neighborhood="רמבם", area='רמב"ם'), "יד2"
    )
    no_name = format_new_listing(
        make_listing(neighborhood=None, street=None, house_number=None, area="רמז"), "יד2"
    )

    assert "📍 הרצל 12, רמב\"ם (לפי המפה: אברמוביץ')" in text
    assert "לפי המפה" not in same
    assert "📍 שכונת רמז" in no_name


def test_message_escapes_html_and_handles_missing_values():
    listing = make_listing(
        price=None, sqm=None, floor=0, street="<b>", house_number=None, neighborhood=None
    )
    text = format_new_listing(listing, "יד2")

    assert "מחיר לא צוין" in text
    assert "4 חד׳ · קומת קרקע" in text
    assert "📍 &lt;b&gt;" in text


def test_price_drop_message():
    text = format_price_drop(make_listing(price=6000), "יד2", 6500, 6000)

    assert text.splitlines()[:2] == ["📉 <b>ירידת מחיר ביד2</b>", "<s>6,500</s> ← <b>6,000 ₪</b>"]


def test_keyboard_reflects_the_users_status():
    keyboard = listing_keyboard(make_listing(), ListingStatus.FAVORITE).inline_keyboard

    assert keyboard[0][0].url == "https://www.yad2.co.il/realestate/item/abc"
    assert [button.text for button in keyboard[1]] == ["✅ במועדפים", "🙈 לא רלוונטי"]
    assert [button.callback_data for button in keyboard[1]] == ["fav:7", "hide:7"]
    assert len(keyboard) == 2  # one photo, and the message shows it


def test_keyboard_links_to_directions_from_the_apartment():
    listing = make_listing(city="ראשון לציון")
    by_point = make_listing(house_number=None, latitude=31.97, longitude=34.79)
    nowhere = make_listing(street=None, house_number=None, neighborhood=None)

    [_, directions] = listing_keyboard(listing, "new").inline_keyboard[0]
    [_, from_point] = listing_keyboard(by_point, "new").inline_keyboard[0]

    assert directions.text == "🧭 מסלול מכאן"
    assert parse_qs(urlsplit(directions.url).query) == {
        "api": ["1"],
        "origin": ["הרצל 12, ראשון לציון"],
    }
    assert directions.url.startswith("https://www.google.com/maps/dir/?")
    assert parse_qs(urlsplit(from_point.url).query)["origin"] == ["31.97,34.79"]
    [_, to_work] = listing_keyboard(listing, "new", "רוטשילד 1, תל אביב").inline_keyboard[0]
    assert parse_qs(urlsplit(to_work.url).query)["destination"] == ["רוטשילד 1, תל אביב"]
    assert len(listing_keyboard(nowhere, "new").inline_keyboard[0]) == 1


def test_keyboard_offers_the_rest_of_the_photos():
    photos = [f"https://img.yad2.co.il/Pic/{index}.jpeg" for index in range(15)]

    [more] = listing_keyboard(make_listing(images=photos), "new").inline_keyboard[1]
    [one_more] = listing_keyboard(make_listing(images=photos[:2]), "new").inline_keyboard[1]

    assert (more.text, more.callback_data) == ("📷 עוד 10 תמונות", "photos:7")
    assert one_more.text == "📷 עוד תמונה"


async def test_each_link_allows_one_more_user_and_users_can_be_removed(service, owner):
    assert await service.pair(service.pairing.create(owner.id), 1, 1, "First")
    assert await service.pair(service.pairing.create(owner.id), 2, 2, "Second")
    assert sorted(await service.allowed_chat_ids()) == [1, 2]

    await service.unpair(1)

    assert await service.allowed_chat_ids() == [2]
    assert not await service.is_allowed(1)
    assert not await service.pair("wrong-code", 3, 3, "Third")


async def test_start_with_a_valid_code_pairs_the_chat(service, owner):
    update, reply = start_update(OWNER_ID)
    context = SimpleNamespace(args=[service.pairing.create(owner.id)])

    await service._on_start(update, context)

    assert await service.is_allowed(OWNER_ID)
    assert "מחובר" in reply.calls[0][0][0]


async def test_pause_and_resume_say_when_nothing_changed(paired):
    replies = []
    for command in ("resume", "pause", "pause", "resume"):
        update, reply = start_update(OWNER_ID)
        await getattr(paired, f"_on_{command}")(update, SimpleNamespace(args=[]))
        replies.append(reply.calls[0][0][0])

    assert replies == [
        "🔔 ההתראות שלך כבר מופעלות.",
        "🔕 ההתראות שלך הושהו. הבדיקות ממשיכות ברקע (/resume).",
        "🔕 ההתראות שלך כבר מושהות (/resume כדי להחזיר אותן).",
        "🔔 ההתראות שלך חזרו.",
    ]


async def test_pausing_pauses_only_the_chats_own_account(paired, owner, session):
    other = await add_account(session, "dan")
    owner_id, other_id = owner.id, other.id

    update, _ = start_update(OWNER_ID)
    await paired._on_pause(update, SimpleNamespace(args=[]))

    session.expire_all()
    assert (await session.get(User, owner_id)).preferences["notifications_paused"] is True
    assert (await session.get(User, other_id)).preferences is None


async def test_strangers_get_no_answer(paired):
    update, reply = start_update(STRANGER_ID)

    await paired._on_start(update, SimpleNamespace(args=[]))
    await paired._on_start(update, SimpleNamespace(args=["guess"]))
    await paired._on_status(update, SimpleNamespace(args=[]))

    assert reply.calls == []
    assert not await paired.is_allowed(STRANGER_ID)


async def test_group_chats_cannot_pair(service, owner):
    update, reply = start_update(OWNER_ID, chat_type="group")

    await service._on_start(update, SimpleNamespace(args=[service.pairing.create(owner.id)]))

    assert await service.allowed_chat_ids() == []
    assert reply.calls == []


async def test_new_listing_is_sent_with_photo_and_recorded(paired, owner, session):
    listing = make_listing(id=None)
    session.add(listing)
    await session.commit()

    await paired.new_listing(owner.id, listing, silent=True)

    [photo] = paired.bot.photos
    assert photo["chat_id"] == OWNER_ID
    assert photo["photo"] == "https://img.yad2.co.il/Pic/1.jpeg"
    assert photo["disable_notification"] is True
    notification = await session.scalar(select(Notification))
    assert (notification.listing_id, notification.kind) == (listing.id, "new")


async def test_listings_go_only_to_the_chats_of_the_user_they_are_for(paired, session):
    other = await add_account(session, "dan")
    assert await paired.pair(paired.pairing.create(other.id), 22, 22, "Dan")
    listing = make_listing(id=None)
    session.add(listing)
    await session.commit()

    await paired.new_listing(other.id, listing, silent=False)
    await paired.message(other.id, "hello")

    assert [photo["chat_id"] for photo in paired.bot.photos] == [22]
    assert [message["chat_id"] for message in paired.bot.messages] == [22]


async def test_photo_failure_falls_back_to_text(paired, owner, session):
    paired.bot = FakeBot(photo_error=BadRequest("Wrong file identifier/http url specified"))
    listing = make_listing(id=None)
    session.add(listing)
    await session.commit()

    await paired.new_listing(owner.id, listing, silent=False)

    assert len(paired.bot.messages) == 1
    assert paired.bot.messages[0]["text"].startswith("🏠")


async def test_nothing_is_sent_before_pairing(service, owner):
    await service.message(owner.id, "hello")

    assert service.bot.messages == []


async def test_chats_without_an_account_are_no_longer_allowed(service, session):
    session.add(TelegramChat(chat_id=5, user_id=5, name="Old"))
    await session.commit()

    assert not await service.is_allowed(5)
    assert await service.allowed_chat_ids() == []


async def test_buttons_toggle_the_users_own_status_for_allowed_users_only(paired, owner, session):
    other = await add_account(session, "dan")
    listing = make_listing(id=None)
    session.add(listing)
    await session.flush()
    session.add(ListingMark(user_id=owner.id, listing_id=listing.id, status=ListingStatus.NEW))
    session.add(ListingMark(user_id=other.id, listing_id=listing.id, status=ListingStatus.NEW))
    await session.commit()
    listing_id, owner_id, other_id = listing.id, owner.id, other.id

    def press(user_id: int, data: str):
        answer, edit = Recorder(), Recorder()
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=data,
            answer=answer,
            edit_message_reply_markup=edit,
        )
        return SimpleNamespace(callback_query=query), answer

    async def status_now(account_id: int = owner_id):
        session.expire_all()
        return (await session.get(ListingMark, (account_id, listing_id))).status

    update, answer = press(STRANGER_ID, f"fav:{listing_id}")
    await paired._on_button(update, None)
    assert await status_now() == ListingStatus.NEW
    assert answer.calls == []

    update, answer = press(OWNER_ID, f"fav:{listing_id}")
    await paired._on_button(update, None)
    assert await status_now() == ListingStatus.FAVORITE
    assert answer.calls[0][0][0] == "⭐ נשמר במועדפים"

    update, _ = press(OWNER_ID, f"fav:{listing_id}")
    await paired._on_button(update, None)
    assert await status_now() == ListingStatus.SEEN
    assert await status_now(other_id) == ListingStatus.NEW


def photos_press(listing_id: int, user_id: int = OWNER_ID):
    answer = Recorder()
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        data=f"photos:{listing_id}",
        answer=answer,
        message=SimpleNamespace(chat=SimpleNamespace(id=OWNER_ID), message_id=55),
    )
    return SimpleNamespace(callback_query=query), answer


async def test_more_photos_button_sends_an_album_in_reply(paired, session):
    photos = [f"https://img.yad2.co.il/Pic/{index}.jpeg" for index in range(4)]
    listing = make_listing(id=None, images=photos)
    session.add(listing)
    await session.commit()

    update, answer = photos_press(listing.id, STRANGER_ID)
    await paired._on_button(update, None)
    assert paired.bot.albums == [] and answer.calls == []

    update, answer = photos_press(listing.id)
    await paired._on_button(update, None)

    [album] = paired.bot.albums
    assert [item.media for item in album["media"]] == photos[1:]
    assert album["reply_parameters"].message_id == 55
    assert len(answer.calls) == 1


async def test_more_photos_are_sent_one_by_one_when_the_album_fails(paired, session):
    paired.bot = FakeBot(album_error=BadRequest("Wrong file identifier/http url specified"))
    photos = [f"https://img.yad2.co.il/Pic/{index}.jpeg" for index in range(3)]
    listing = make_listing(id=None, images=photos)
    session.add(listing)
    await session.commit()

    update, _ = photos_press(listing.id)
    await paired._on_button(update, None)

    assert [photo["photo"] for photo in paired.bot.photos] == photos[1:]

    paired.bot = FakeBot(
        photo_error=BadRequest("Wrong file identifier/http url specified"),
        album_error=BadRequest("Wrong file identifier/http url specified"),
    )
    await paired._on_button(update, None)
    assert paired.bot.messages[0]["text"].startswith("לא הצלחתי לשלוח את התמונות")
