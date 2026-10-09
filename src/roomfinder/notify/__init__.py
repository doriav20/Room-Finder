import logging
from typing import Protocol

from roomfinder.models import Listing

log = logging.getLogger(__name__)


class Notifier(Protocol):
    """Sends to one dashboard user (by account id), on whatever they connected."""

    async def new_listing(self, user_id: int, listing: Listing, *, silent: bool) -> None: ...

    async def price_drop(
        self, user_id: int, listing: Listing, old_price: int, new_price: int, *, silent: bool
    ) -> None: ...

    async def message(self, user_id: int, text: str, *, silent: bool = False) -> None: ...


class LogNotifier:
    """Used when Telegram isn't configured: notifications only go to the log."""

    async def new_listing(self, user_id: int, listing: Listing, *, silent: bool) -> None:
        log.info("New listing for user %s: %s %s", user_id, listing.source, listing.url)

    async def price_drop(
        self, user_id: int, listing: Listing, old_price: int, new_price: int, *, silent: bool
    ) -> None:
        log.info("Price drop for user %s, %s -> %s: %s", user_id, old_price, new_price, listing.url)

    async def message(self, user_id: int, text: str, *, silent: bool = False) -> None:
        log.info("Message for user %s: %s", user_id, text)
