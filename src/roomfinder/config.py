from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

from pydantic_settings import BaseSettings, SettingsConfigDict

# Where the dashboard serves noVNC (see web/novnc.py)
NOVNC_PAGE = "/novnc/vnc.html?autoconnect=1&resize=scale&path=novnc/websockify"


class Settings(BaseSettings):
    """Process configuration from environment variables (see .env.example)."""

    model_config = SettingsConfigDict(extra="ignore")

    data_dir: Path = Path("/data")
    database_url: str | None = None
    cdp_url: str = "http://127.0.0.1:9222"
    telegram_bot_token: str = ""
    scheduler_enabled: bool = True
    log_level: str = "INFO"
    timezone: str = "Asia/Jerusalem"
    # The dashboard's address as users open it; used in links that leave it (invites, Telegram)
    dashboard_url: str = "http://localhost:8000"
    # The dashboard answers only to these host names (guards against DNS rebinding)
    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]
    # Mark the sign-in cookie HTTPS-only; needed behind an HTTPS proxy that talks HTTP to us
    cookie_secure: bool = False

    def link(self, path: str, **params: str) -> str:
        """A full address in the dashboard, for links that are sent or copied elsewhere."""
        query = f"?{urlencode(params)}" if params else ""
        return f"{self.dashboard_url.rstrip('/')}{path}{query}"

    @property
    def novnc_url(self) -> str:
        """The container's browser, inside the dashboard (admins only)."""
        return self.link(NOVNC_PAGE)

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url or f"sqlite+aiosqlite:///{self.data_dir / 'roomfinder.db'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
