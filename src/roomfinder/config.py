from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    dashboard_url: str = "http://localhost:8000"
    novnc_url: str = "http://localhost:6080/vnc.html?autoconnect=1&resize=scale"
    # The dashboard answers only to these host names (guards against DNS rebinding)
    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url or f"sqlite+aiosqlite:///{self.data_dir / 'roomfinder.db'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
