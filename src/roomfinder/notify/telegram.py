"""Telegram bot: sends listings to the allowed users and handles buttons and commands.

Telegram bots are public, so access is enforced here. A Telegram user is allowed only after
opening a one-time link that a signed-in dashboard user created for their own account. Every
update is checked against the allowed Telegram user ids; anyone else is ignored without a
reply. When a dashboard account is disabled, its Telegram chats are removed with it.

Each chat acts for its account: it gets the listings that fit that user's search, and its
buttons and commands change that user's marks and settings only.
"""

import html
import logging
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
    Message,
    ReplyParameters,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from roomfinder.config import Settings
from roomfinder.db import utcnow
from roomfinder.labels import area_note, directions_url, furniture_label, parking_label
from roomfinder.models import (
    Listing,
    ListingMark,
    ListingStatus,
    Notification,
    SourceState,
    TelegramChat,
    User,
)
from roomfinder.schemas import PROPERTY_TYPE_LABELS, PropertyType
from roomfinder.settings_store import set_user_preferences, user_preferences

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
# Telegram's most photos in one album
ALBUM_LIMIT = 10
PAIRING_TTL = timedelta(minutes=10)
HELP_TEXT = (
    "פקודות:\n"
    "/status - מצב המקורות\n"
    "/latest - המודעות האחרונות שמתאימות לחיפוש שלך\n"
    "/pause - השהיית ההתראות שלך\n"
    "/resume - חידוש ההתראות שלך"
)


@dataclass(frozen=True)
class PairingRequest:
    expires_at: datetime
    # The dashboard account the Telegram chat will belong to
    account_id: int


class PairingCodes:
    """Short-lived one-time codes that let a Telegram user join the allowed list."""

    def __init__(self, ttl: timedelta = PAIRING_TTL) -> None:
        self.ttl = ttl
        self._pending: dict[str, PairingRequest] = {}

    def create(self, account_id: int) -> str:
        code = secrets.token_urlsafe(16)
        self._pending[code] = PairingRequest(utcnow() + self.ttl, account_id)
        return code

    def consume(self, code: str) -> PairingRequest | None:
        request = self._pending.pop(code, None)
        return request if request is not None and request.expires_at > utcnow() else None


def _rooms(value: float | None) -> str | None:
    return f"{value:g} חד׳" if value else None


def _floor(value: int | None) -> str | None:
    if value is None:
        return None
    return "קומת קרקע" if value == 0 else f"קומה {value}"


def _price(value: int | None) -> str:
    return f"{value:,} ₪" if value else "מחיר לא צוין"


def _escape(text: str) -> str:
    # Text nodes only; quotes are common in Hebrew abbreviations (ממ"ד) and safe here
    return html.escape(text, quote=False)


def _listing_details(listing: Listing) -> list[str]:
    e = _escape
    lines = []
    specs = [_rooms(listing.rooms), f"{listing.sqm} מ״ר" if listing.sqm else None]
    specs.append(_floor(listing.floor))
    if specs := [spec for spec in specs if spec]:
        lines.append(" · ".join(specs))
    street = " ".join(filter(None, [listing.street, listing.house_number]))
    place = ", ".join(filter(None, [street, listing.neighborhood]))
    if note := area_note(listing):
        place = f"{place} (לפי המפה: {note})" if place else f"שכונת {note}"
    if place:
        lines.append(f"📍 {e(place)}")
    kind = {True: "תיווך", False: "פרטי"}.get(listing.is_agency)
    property_label = (
        PROPERTY_TYPE_LABELS.get(PropertyType(listing.property_type))
        if listing.property_type in PropertyType
        else None
    )
    if tags := [tag for tag in (kind, property_label) if tag]:
        lines.append("🏷 " + " · ".join(tags))
    extras = []
    if furniture := furniture_label(listing):
        extras.append(f"🛋 {furniture}")
    if parking := parking_label(listing):
        extras.append(f"🚗 {parking}")
    if extras:
        lines.append(" · ".join(extras))
    features = listing.features
    if parking:  # already said above
        features = [feature for feature in features if feature != "חניה"]
    if features:
        lines.append("✨ " + e(" · ".join(features[:6])))
    return lines


def format_new_listing(listing: Listing, source_label: str) -> str:
    lines = [f"🏠 <b>דירה חדשה ב{_escape(source_label)}</b>", f"<b>{_price(listing.price)}</b>"]
    return "\n".join(lines + _listing_details(listing))[:CAPTION_LIMIT]


def format_price_drop(listing: Listing, source_label: str, old_price: int, new_price: int) -> str:
    lines = [
        f"📉 <b>ירידת מחיר ב{_escape(source_label)}</b>",
        f"<s>{old_price:,}</s> ← <b>{new_price:,} ₪</b>",
    ]
    return "\n".join(lines + _listing_details(listing))[:CAPTION_LIMIT]


def more_photos(listing: Listing) -> list[str]:
    """The listing's photos after the one its message shows, as many as fit in an album."""
    return listing.images[1 : 1 + ALBUM_LIMIT]


def listing_keyboard(listing: Listing, status: str) -> InlineKeyboardMarkup:
    """The buttons under a listing, lit by the status the chat's user gave it."""
    favorite = status == ListingStatus.FAVORITE
    hidden = status == ListingStatus.HIDDEN
    top = [InlineKeyboardButton("🔗 למודעה", url=listing.url)]
    # Google Maps opens with the apartment as the start; the destination is typed there
    if directions := directions_url(listing):
        top.append(InlineKeyboardButton("🧭 מסלול מכאן", url=directions))
    rows = [top]
    # An album can't carry buttons, so the rest of the photos come on request
    if photos := more_photos(listing):
        label = f"📷 עוד {len(photos)} תמונות" if len(photos) > 1 else "📷 עוד תמונה"
        rows.append([InlineKeyboardButton(label, callback_data=f"photos:{listing.id}")])
    return InlineKeyboardMarkup(
        [
            *rows,
            [
                InlineKeyboardButton(
                    "✅ במועדפים" if favorite else "⭐ שמור", callback_data=f"fav:{listing.id}"
                ),
                InlineKeyboardButton(
                    "✅ לא רלוונטי" if hidden else "🙈 לא רלוונטי",
                    callback_data=f"hide:{listing.id}",
                ),
            ],
        ]
    )


class TelegramService:
    """Implements the Notifier protocol and runs the interactive bot."""

    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        source_labels: dict[str, str],
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.source_labels = source_labels
        self.pairing = PairingCodes()
        self.username: str | None = None
        self.application = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .rate_limiter(AIORateLimiter())
            .build()
        )
        self.bot = self.application.bot
        for command, callback in {
            "start": self._on_start,
            "help": self._on_help,
            "status": self._on_status,
            "latest": self._on_latest,
            "pause": self._on_pause,
            "resume": self._on_resume,
        }.items():
            self.application.add_handler(CommandHandler(command, callback))
        self.application.add_handler(CallbackQueryHandler(self._on_button))

    async def start(self, *, polling: bool = True) -> None:
        await self.application.initialize()
        self.username = (await self.bot.get_me()).username
        if polling:
            await self.application.start()
            await self.application.updater.start_polling(
                drop_pending_updates=True, allowed_updates=["message", "callback_query"]
            )
        log.info("Telegram bot @%s is up", self.username)

    async def stop(self) -> None:
        if self.application.updater and self.application.updater.running:
            await self.application.updater.stop()
        if self.application.running:
            await self.application.stop()
        await self.application.shutdown()

    def pairing_link(self, account_id: int) -> str:
        return f"https://t.me/{self.username}?start={self.pairing.create(account_id)}"

    # --- access ---------------------------------------------------------------

    @staticmethod
    def _allowed_chats():
        # Every chat acts for an account; chats of disabled accounts don't count (they're also
        # deleted when disabling), nor chats paired before there were accounts
        return (
            select(TelegramChat)
            .join(User, User.id == TelegramChat.account_id)
            .where(User.is_active.is_(True))
        )

    async def allowed_chat_ids(self) -> list[int]:
        async with self.sessionmaker() as session:
            return [chat.chat_id for chat in await session.scalars(self._allowed_chats())]

    async def chat_ids_of(self, account_id: int) -> list[int]:
        async with self.sessionmaker() as session:
            chats = await session.scalars(
                self._allowed_chats().where(TelegramChat.account_id == account_id)
            )
            return [chat.chat_id for chat in chats]

    async def account_of(self, user_id: int | None) -> int | None:
        """The dashboard account an allowed Telegram user acts for, or None for anyone else."""
        if user_id is None:
            return None
        async with self.sessionmaker() as session:
            chat = await session.scalar(
                self._allowed_chats().where(TelegramChat.user_id == user_id).limit(1)
            )
            return chat.account_id if chat else None

    async def is_allowed(self, user_id: int | None) -> bool:
        return await self.account_of(user_id) is not None

    async def pair(self, code: str, chat_id: int, user_id: int, name: str) -> bool:
        """Allow this user, if the one-time code is valid."""
        request = self.pairing.consume(code)
        if request is None:
            return False
        async with self.sessionmaker() as session:
            await session.merge(
                TelegramChat(
                    chat_id=chat_id, user_id=user_id, name=name, account_id=request.account_id
                )
            )
            await session.commit()
        log.info("Telegram user %s (%s) is now allowed", name, user_id)
        return True

    async def unpair(self, chat_id: int) -> None:
        async with self.sessionmaker() as session:
            await session.execute(delete(TelegramChat).where(TelegramChat.chat_id == chat_id))
            await session.commit()

    # --- Notifier ------------------------------------------------------------

    async def new_listing(self, user_id: int, listing: Listing, *, silent: bool) -> None:
        text = format_new_listing(listing, self._label(listing))
        await self._send_listing(user_id, listing, text, "new", silent)

    async def price_drop(
        self, user_id: int, listing: Listing, old_price: int, new_price: int, *, silent: bool
    ) -> None:
        text = format_price_drop(listing, self._label(listing), old_price, new_price)
        await self._send_listing(user_id, listing, text, "price_drop", silent)

    async def message(self, user_id: int, text: str, *, silent: bool = False) -> None:
        await self.send_text(await self.chat_ids_of(user_id), text, silent=silent)

    async def send_text(self, chat_ids: Iterable[int], text: str, *, silent: bool = False) -> None:
        for chat_id in chat_ids:
            await self.bot.send_message(
                chat_id,
                text,
                disable_notification=silent,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

    def _label(self, listing: Listing) -> str:
        return self.source_labels.get(listing.source, listing.source)

    async def _send_listing(
        self, user_id: int, listing: Listing, text: str, kind: str, silent: bool
    ) -> None:
        async with self.sessionmaker() as session:
            status = await session.scalar(
                select(ListingMark.status).where(
                    ListingMark.user_id == user_id, ListingMark.listing_id == listing.id
                )
            )
        keyboard = listing_keyboard(listing, status or ListingStatus.SEEN)
        for chat_id in await self.chat_ids_of(user_id):
            sent: Message | None = None
            if listing.images:
                try:
                    sent = await self.bot.send_photo(
                        chat_id,
                        photo=listing.images[0],
                        caption=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                        disable_notification=silent,
                    )
                except BadRequest as exc:
                    log.info("Sending the photo failed (%s); sending text only", exc)
            if sent is None:
                sent = await self.bot.send_message(
                    chat_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_notification=silent,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            async with self.sessionmaker() as session:
                session.add(
                    Notification(
                        listing_id=listing.id,
                        chat_id=chat_id,
                        kind=kind,
                        message_id=sent.message_id,
                    )
                )
                await session.commit()

    # --- handlers ------------------------------------------------------------

    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat, user, message = update.effective_chat, update.effective_user, update.message
        if chat is None or user is None or message is None or chat.type != ChatType.PRIVATE:
            return
        if context.args and await self.pair(context.args[0], chat.id, user.id, user.full_name):
            await message.reply_text(
                "✅ מחובר! מעכשיו יגיעו לכאן התראות על דירות חדשות שמתאימות לחיפוש שלך.\n\n"
                + HELP_TEXT
            )
        elif await self.is_allowed(user.id):
            await message.reply_text("הבוט פעיל ✅\n\n" + HELP_TEXT)
        # Anyone else gets no answer at all

    async def _on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._account(update) is not None:
            await update.message.reply_text(HELP_TEXT)

    async def _on_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        account_id = await self._account(update)
        if account_id is None:
            return
        timezone = ZoneInfo(self.settings.timezone)
        lines = []
        async with self.sessionmaker() as session:
            account = await session.get(User, account_id)
            paused = user_preferences(account).notifications_paused
            states = {state.source: state for state in await session.scalars(select(SourceState))}
        for name, label in self.source_labels.items():
            state = states.get(name)
            if state is None or state.last_run_at is None:
                lines.append(f"⏳ {label}: עוד לא נבדק")
                continue
            icon = "✅" if state.consecutive_failures == 0 else "⚠️"
            if not state.enabled:
                icon = "⏸"
            last = state.last_success_at or state.last_run_at
            lines.append(f"{icon} {label}: {last.astimezone(timezone):%d/%m %H:%M}")
            if state.last_error:
                lines.append(f"    {state.last_error[:120]}")
        if paused:
            lines.append("\n🔕 ההתראות שלך מושהות (/resume לחידוש)")
        await update.message.reply_text("\n".join(lines))

    async def _on_latest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        account_id = await self._account(update)
        if account_id is None:
            return
        async with self.sessionmaker() as session:
            listings = list(
                await session.scalars(
                    select(Listing)
                    .join(ListingMark, ListingMark.listing_id == Listing.id)
                    .where(
                        ListingMark.user_id == account_id,
                        ListingMark.matches.is_(True),
                        ListingMark.status != ListingStatus.HIDDEN,
                        Listing.removed_at.is_(None),
                        Listing.duplicate_of_id.is_(None),
                    )
                    .order_by(Listing.first_seen_at.desc())
                    .limit(5)
                )
            )
        if not listings:
            await update.message.reply_text("אין עדיין מודעות שמתאימות לחיפוש שלך.")
            return
        lines = []
        for listing in listings:
            place = " ".join(filter(None, [listing.street, listing.house_number])) or "ללא כתובת"
            label = _escape(f"{_price(listing.price)} · {place}")
            lines.append(f'• <a href="{html.escape(listing.url)}">{label}</a>')
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def _on_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        account_id = await self._account(update)
        if account_id is not None:
            if await self._set_paused(account_id, True):
                text = "🔕 ההתראות שלך הושהו. הבדיקות ממשיכות ברקע (/resume)."
            else:
                text = "🔕 ההתראות שלך כבר מושהות (/resume כדי להחזיר אותן)."
            await update.message.reply_text(text)

    async def _on_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        account_id = await self._account(update)
        if account_id is not None:
            if await self._set_paused(account_id, False):
                text = "🔔 ההתראות שלך חזרו."
            else:
                text = "🔔 ההתראות שלך כבר מופעלות."
            await update.message.reply_text(text)

    async def _set_paused(self, account_id: int, paused: bool) -> bool:
        """Pause or resume one user's notifications; False when they were already that way."""
        async with self.sessionmaker() as session:
            account = await session.get(User, account_id)
            preferences = user_preferences(account)
            if preferences.notifications_paused == paused:
                return False
            preferences.notifications_paused = paused
            set_user_preferences(account, preferences)
            await session.commit()
        return True

    async def _on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        account_id = await self.account_of(query.from_user.id)
        if account_id is None:
            return
        action, _, listing_id = (query.data or "").partition(":")
        if action == "photos" and listing_id.isdigit():
            await self._send_more_photos(query, int(listing_id))
            return
        wanted = {"fav": ListingStatus.FAVORITE, "hide": ListingStatus.HIDDEN}.get(action)
        if wanted is None or not listing_id.isdigit():
            await query.answer()
            return
        async with self.sessionmaker() as session:
            listing = await session.get(Listing, int(listing_id))
            if listing is None:
                await query.answer("המודעה לא נמצאה")
                return
            found = listing.marks.get(account_id)
            if found is None:
                found = listing.marks[account_id] = ListingMark(
                    user_id=account_id, matches=False, status=ListingStatus.SEEN
                )
            # Pressing the same button again undoes it
            found.status = ListingStatus.SEEN if found.status == wanted else wanted
            status = found.status
            await session.commit()
        answers = {
            ListingStatus.FAVORITE: "⭐ נשמר במועדפים",
            ListingStatus.HIDDEN: "🙈 סומן כלא רלוונטי",
        }
        await query.answer(answers.get(ListingStatus(status), "בוטל"))
        await query.edit_message_reply_markup(listing_keyboard(listing, status))

    async def _send_more_photos(self, query: CallbackQuery, listing_id: int) -> None:
        """Send the rest of a listing's photos as a reply to its message."""
        async with self.sessionmaker() as session:
            listing = await session.get(Listing, listing_id)
        photos = more_photos(listing) if listing else []
        if not photos or query.message is None:
            await query.answer("אין עוד תמונות")
            return
        await query.answer()
        chat_id = query.message.chat.id
        reply = ReplyParameters(
            message_id=query.message.message_id, allow_sending_without_reply=True
        )
        if len(photos) > 1:
            try:
                await self.bot.send_media_group(
                    chat_id,
                    [InputMediaPhoto(photo) for photo in photos],
                    disable_notification=True,
                    reply_parameters=reply,
                )
                return
            except BadRequest as exc:
                # One photo Telegram can't fetch fails the whole album
                log.info("Sending the album failed (%s); sending the photos one by one", exc)
        sent = 0
        for photo in photos:
            try:
                await self.bot.send_photo(
                    chat_id, photo=photo, disable_notification=True, reply_parameters=reply
                )
                sent += 1
            except BadRequest as exc:
                log.info("Sending a photo failed (%s)", exc)
        if not sent:
            await self.bot.send_message(
                chat_id,
                "לא הצלחתי לשלוח את התמונות. אפשר לראות אותן במודעה.",
                reply_parameters=reply,
            )

    async def _account(self, update: Update) -> int | None:
        """The account a command message acts for, if it comes from an allowed user."""
        user = update.effective_user
        if update.message is None or user is None:
            return None
        return await self.account_of(user.id)
