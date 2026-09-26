from fastapi.testclient import TestClient

from roomfinder.config import Settings
from roomfinder.main import create_app


def test_health_returns_ok():
    app = create_app(Settings(database_url="sqlite+aiosqlite://", scheduler_enabled=False))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "browser_connected": False, "telegram": False}


def test_pairing_link_needs_telegram():
    app = create_app(Settings(database_url="sqlite+aiosqlite://", scheduler_enabled=False))

    with TestClient(app) as client:
        response = client.post("/telegram/pairing-link")

    assert response.status_code == 503
