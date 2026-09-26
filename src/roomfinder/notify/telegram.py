"""Telegram bot: sends listings to the allowed users and handles buttons and commands.

Telegram bots are public, so access is enforced here. A user is allowed only after opening a
one-time link created in the dashboard (which is reachable from this computer only). Every
update is checked against the allowed Telegram user ids; anyone else is ignored without a
reply.
"""

import html
import logging
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message, Update
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
from roomfinder.models import Listing, ListingStatus, Notification, SourceState, TelegramChat
from roomfinder.schemas import PROPERTY_TYPE_LABELS, PropertyType
from roomfinder.settings_store import get_preferences, save_preferences

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
PAIRING_TTL = timedelta(minutes=10)
HELP_TEXT = (
    "פקודות:\n"
    "/status - מצב המקורות\n"
    "/latest - המודעות המתאימות האחרונות\n"
    "/pause - השהיית התראות\n"
    "/resume - חידוש התראות"
)


class PairingCodes:
    """Short-lived one-time codes that let a Telegram user join the allowed list."""

    def __init__(self, ttl: timedelta = PAIRING_TTL) -> None:
        self.ttl = ttl
        self._expiry: dict[str, datetime] = {}

    def create(self) -> str:
        code = secrets.token_urlsafe(16)
        self._expiry[code] = utcnow() + self.ttl
        return code

    def consume(self, code: str) -> bool:
        expiry = self._expiry.pop(code, None)
        return expiry is not None and expiry > utcnow()


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
    if place := ", ".join(filter(None, [street, listing.neighborhood])):
        lines.append(f"📍 {e(place)}")
    kind = {True: "תיווך", False: "פרטי"}.get(listing.is_agency)
    property_label = (
        PROPERTY_TYPE_LABELS.get(PropertyType(listing.property_type))
        if listing.property_type in PropertyType
        else None
    )
    if tags := [tag for tag in (kind, property_label) if tag]:
        lines.append("🏷 " + " · ".join(tags))
    if listing.features:
        lines.append("✨ " + e(" · ".join(listing.features[:6])))
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


def listing_keyboard(listing: Listing) -> InlineKeyboardMarkup:
    favorite = listing.status == ListingStatus.FAVORITE
    hidden = listing.status == ListingStatus.HIDDEN
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 למודעה", url=listing.url)],
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

    def pairing_link(self) -> str:
        return f"https://t.me/{self.username}?start={self.pairing.create()}"

    # --- access ---------------------------------------------------------------

    async def allowed_chat_ids(self) -> list[int]:
        async with self.sessionmaker() as session:
            return list(await session.scalars(select(TelegramChat.chat_id)))

    async def is_allowed(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        async with self.sessionmaker() as session:
            found = await session.scalar(
                select(TelegramChat.chat_id).where(TelegramChat.user_id == user_id)
            )
            return found is not None

    async def pair(self, code: str, chat_id: int, user_id: int, name: str) -> bool:
        """Allow this user, if the one-time code is valid."""
        if not self.pairing.consume(code):
            return False
        async with self.sessionmaker() as session:
            await session.merge(TelegramChat(chat_id=chat_id, user_id=user_id, name=name))
            await session.commit()
        log.info("Telegram user %s (%s) is now allowed", name, user_id)
        return True

    async def unpair(self, chat_id: int) -> None:
        async with self.sessionmaker() as session:
            await session.execute(delete(TelegramChat).where(TelegramChat.chat_id == chat_id))
            await session.commit()

    # --- Notifier ------------------------------------------------------------

    async def new_listing(self, listing: Listing, *, silent: bool) -> None:
        text = format_new_listing(listing, self._label(listing))
        await self._send_listing(listing, text, "new", silent)

    async def price_drop(
        self, listing: Listing, old_price: int, new_price: int, *, silent: bool
    ) -> None:
        text = format_price_drop(listing, self._label(listing), old_price, new_price)
        await self._send_listing(listing, text, "price_drop", silent)

    async def message(self, text: str, *, silent: bool = False) -> None:
        for chat_id in await self.allowed_chat_ids():
            await self.bot.send_message(
                chat_id,
                text,
                disable_notification=silent,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

    def _label(self, listing: Listing) -> str:
        return self.source_labels.get(listing.source, listing.source)

    async def _send_listing(self, listing: Listing, text: str, kind: str, silent: bool) -> None:
        keyboard = listing_keyboard(listing)
        for chat_id in await self.allowed_chat_ids():
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
                "✅ מחובר! מעכשיו יגיעו לכאן התראות על דירות חדשות.\n\n" + HELP_TEXT
            )
        elif await self.is_allowed(user.id):
            await message.reply_text("הבוט פעיל ✅\n\n" + HELP_TEXT)
        # Anyone else gets no answer at all

    async def _on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._allowed_message(update):
            await update.message.reply_text(HELP_TEXT)

    async def _on_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed_message(update):
            return
        timezone = ZoneInfo(self.settings.timezone)
        lines = []
        async with self.sessionmaker() as session:
            paused = (await get_preferences(session)).notifications_paused
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
            lines.append("\n🔕 ההתראות מושהות (/resume לחידוש)")
        await update.message.reply_text("\n".join(lines))

    async def _on_latest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed_message(update):
            return
        async with self.sessionmaker() as session:
            listings = list(
                await session.scalars(
                    select(Listing)
                    .where(
                        Listing.matches.is_(True),
                        Listing.removed_at.is_(None),
                        Listing.duplicate_of_id.is_(None),
                        Listing.status != ListingStatus.HIDDEN,
                    )
                    .order_by(Listing.first_seen_at.desc())
                    .limit(5)
                )
            )
        if not listings:
            await update.message.reply_text("אין עדיין מודעות מתאימות.")
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
        if await self._allowed_message(update):
            await self._set_paused(True)
            await update.message.reply_text("🔕 ההתראות הושהו. הבדיקות ממשיכות ברקע (/resume).")

    async def _on_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._allowed_message(update):
            await self._set_paused(False)
            await update.message.reply_text("🔔 ההתראות חזרו.")

    async def _set_paused(self, paused: bool) -> None:
        async with self.sessionmaker() as session:
            preferences = await get_preferences(session)
            preferences.notifications_paused = paused
            await save_preferences(session, preferences)
            await session.commit()

    async def _on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not await self.is_allowed(query.from_user.id):
            return
        action, _, listing_id = (query.data or "").partition(":")
        wanted = {"fav": ListingStatus.FAVORITE, "hide": ListingStatus.HIDDEN}.get(action)
        if wanted is None or not listing_id.isdigit():
            await query.answer()
            return
        async with self.sessionmaker() as session:
            listing = await session.get(Listing, int(listing_id))
            if listing is None:
                await query.answer("המודעה לא נמצאה")
                return
            # Pressing the same button again undoes it
            listing.status = ListingStatus.SEEN if listing.status == wanted else wanted
            await session.commit()
        answers = {
            ListingStatus.FAVORITE: "⭐ נשמר במועדפים",
            ListingStatus.HIDDEN: "🙈 סומן כלא רלוונטי",
        }
        await query.answer(answers.get(ListingStatus(listing.status), "בוטל"))
        await query.edit_message_reply_markup(listing_keyboard(listing))

    async def _allowed_message(self, update: Update) -> bool:
        user = update.effective_user
        return update.message is not None and user is not None and await self.is_allowed(user.id)
