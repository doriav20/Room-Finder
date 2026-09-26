"""Command line tools, run inside the container, e.g.:

docker compose exec app python -m roomfinder fetch yad2
docker compose exec app python -m roomfinder invite --admin
"""

import argparse
import asyncio
import contextlib
from collections.abc import AsyncIterator
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from roomfinder import auth
from roomfinder.browser import BrowserManager
from roomfinder.config import Settings, get_settings
from roomfinder.db import create_engine, create_sessionmaker
from roomfinder.main import configure_logging
from roomfinder.models import InviteKind, Role, User
from roomfinder.notify import LogNotifier, Notifier
from roomfinder.notify.telegram import TelegramService
from roomfinder.runner import SourceRunner
from roomfinder.settings_store import get_criteria
from roomfinder.sources import build_sources


@contextlib.asynccontextmanager
async def notifier_for(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Notifier]:
    """Telegram when configured (sending only - the app's bot does the polling), else the log."""
    if not settings.telegram_bot_token:
        yield LogNotifier()
        return
    labels = {name: source.label for name, source in build_sources().items()}
    telegram = TelegramService(settings, sessionmaker, labels)
    await telegram.start(polling=False)
    try:
        yield telegram
    finally:
        await telegram.stop()


async def fetch(source_name: str, limit: int) -> None:
    """Load a source and print what it returns, without storing or notifying."""
    settings = get_settings()
    source = build_sources()[source_name]
    engine = create_engine(settings.sqlalchemy_url)
    async with create_sessionmaker(engine)() as session:
        criteria = await get_criteria(session)
    await engine.dispose()

    browser = BrowserManager(settings.cdp_url)
    try:
        async with browser.session() as page:
            result = await source.fetch(page, criteria, None)
    finally:
        await browser.stop()

    print(f"{source.label}: {len(result.listings)} listings, complete={result.complete}")
    for listing in result.listings[:limit]:
        address = " ".join(filter(None, [listing.street, listing.house_number]))
        print(
            f"  {listing.price or '?':>6} ₪ | {listing.rooms} rooms | {listing.sqm or '?'} m² | "
            f"floor {listing.floor} | {address}, {listing.neighborhood} | {listing.url}"
        )


async def run(source_name: str) -> None:
    """One full check of a source: fetch, store and notify."""
    settings = get_settings()
    engine = create_engine(settings.sqlalchemy_url)
    sessionmaker = create_sessionmaker(engine)
    browser = BrowserManager(settings.cdp_url)
    try:
        async with notifier_for(settings, sessionmaker) as notifier:
            runner = SourceRunner(sessionmaker, browser, notifier, build_sources(), settings)
            print(await runner.run(source_name))
    finally:
        await browser.stop()
        await engine.dispose()


async def notify_test() -> None:
    settings = get_settings()
    engine = create_engine(settings.sqlalchemy_url)
    sessionmaker = create_sessionmaker(engine)
    try:
        async with notifier_for(settings, sessionmaker) as notifier:
            if isinstance(notifier, TelegramService) and not await notifier.allowed_chat_ids():
                print("No Telegram user is connected yet - create a link in the dashboard")
            await notifier.message("✅ הודעת בדיקה מ-RoomFinder")
    finally:
        await engine.dispose()


async def invite(admin: bool, note: str | None) -> None:
    """Print a one-time sign-up link. The first admin's link can only come from here."""
    settings = get_settings()
    engine = create_engine(settings.sqlalchemy_url)
    try:
        async with create_sessionmaker(engine)() as session:
            role = Role.ADMIN if admin else Role.MEMBER
            token, invite_row = await auth.create_invite(session, role=role, note=note)
            await session.commit()
    finally:
        await engine.dispose()
    link = f"{settings.dashboard_url.rstrip('/')}/register?{urlencode({'token': token})}"
    kind = "admin" if admin else "member"
    print(
        f"One-time sign-up link ({kind}), valid until {invite_row.expires_at:%Y-%m-%d %H:%M} UTC:"
    )
    print(link)


async def list_users() -> None:
    settings = get_settings()
    engine = create_engine(settings.sqlalchemy_url)
    try:
        async with create_sessionmaker(engine)() as session:
            users = list(await session.scalars(select(User).order_by(User.created_at)))
    finally:
        await engine.dispose()
    if not users:
        print("No users yet - create the first admin with: python -m roomfinder invite --admin")
    for user in users:
        state = "active" if user.is_active else "disabled"
        print(f"{user.username:<32} {user.role:<8} {state}")


async def reset_link(username: str) -> None:
    """Print a one-time link that sets a new password (e.g. for a forgotten admin password)."""
    settings = get_settings()
    engine = create_engine(settings.sqlalchemy_url)
    try:
        async with create_sessionmaker(engine)() as session:
            user = await session.scalar(
                select(User).where(User.username_key == auth.username_key(username))
            )
            if user is None or not user.is_active:
                raise SystemExit(f"No active user named {username!r}")
            token, _ = await auth.create_invite(session, kind=InviteKind.RESET, user=user)
            await session.commit()
    finally:
        await engine.dispose()
    print(f"One-time password reset link for {user.username}, valid for 24 hours:")
    print(f"{settings.dashboard_url.rstrip('/')}/reset?{urlencode({'token': token})}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="roomfinder")
    commands = parser.add_subparsers(dest="command", required=True)
    source_names = list(build_sources())

    invite_parser = commands.add_parser("invite", help="print a one-time sign-up link")
    invite_parser.add_argument("--admin", action="store_true", help="the new user is an admin")
    invite_parser.add_argument("--note", help="who the link is for")

    commands.add_parser("users", help="list the dashboard users")

    reset_parser = commands.add_parser("reset-link", help="print a one-time password reset link")
    reset_parser.add_argument("username")

    fetch_parser = commands.add_parser("fetch", help="print a source's listings (dry run)")
    fetch_parser.add_argument("source", choices=source_names)
    fetch_parser.add_argument("--limit", type=int, default=10, help="listings to print")

    run_parser = commands.add_parser("run", help="check a source: store and notify")
    run_parser.add_argument("source", choices=source_names)

    commands.add_parser("notify-test", help="send a test message to the connected users")

    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level, [settings.telegram_bot_token])
    if args.command == "fetch":
        asyncio.run(fetch(args.source, args.limit))
    elif args.command == "run":
        asyncio.run(run(args.source))
    elif args.command == "notify-test":
        asyncio.run(notify_test())
    elif args.command == "invite":
        asyncio.run(invite(args.admin, args.note))
    elif args.command == "users":
        asyncio.run(list_users())
    elif args.command == "reset-link":
        asyncio.run(reset_link(args.username))
