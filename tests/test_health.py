from fastapi.testclient import TestClient

from roomfinder.config import Settings
from roomfinder.main import create_app


def test_health_returns_ok(database_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        scheduler_enabled=False,
        allowed_hosts=["testserver"],
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "browser_connected": False, "telegram": False}
