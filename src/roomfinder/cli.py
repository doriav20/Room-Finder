"""Command line tools for checking sources by hand, e.g.:

docker compose exec app python -m roomfinder fetch yad2
"""

import argparse
import asyncio

from roomfinder.browser import BrowserManager
from roomfinder.config import get_settings
from roomfinder.db import create_engine, create_sessionmaker
from roomfinder.main import configure_logging
from roomfinder.notify import LogNotifier
from roomfinder.runner import SourceRunner
from roomfinder.settings_store import get_criteria
from roomfinder.sources import build_sources


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
    """One full check of a source: fetch, store and notify (to the log)."""
    settings = get_settings()
    engine = create_engine(settings.sqlalchemy_url)
    browser = BrowserManager(settings.cdp_url)
    runner = SourceRunner(
        create_sessionmaker(engine), browser, LogNotifier(), build_sources(), settings
    )
    try:
        print(await runner.run(source_name))
    finally:
        await browser.stop()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="roomfinder")
    commands = parser.add_subparsers(dest="command", required=True)
    source_names = list(build_sources())

    fetch_parser = commands.add_parser("fetch", help="print a source's listings (dry run)")
    fetch_parser.add_argument("source", choices=source_names)
    fetch_parser.add_argument("--limit", type=int, default=10, help="listings to print")

    run_parser = commands.add_parser("run", help="check a source: store and notify")
    run_parser.add_argument("source", choices=source_names)

    args = parser.parse_args()
    configure_logging(get_settings().log_level)
    if args.command == "fetch":
        asyncio.run(fetch(args.source, args.limit))
    elif args.command == "run":
        asyncio.run(run(args.source))
