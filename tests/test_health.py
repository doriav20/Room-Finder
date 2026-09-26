from fastapi.testclient import TestClient

from roomfinder.config import Settings
from roomfinder.main import create_app


def make_app(database_path):
    return create_app(
        Settings(
            database_url=f"sqlite+aiosqlite:///{database_path}",
            scheduler_enabled=False,
            allowed_hosts=["testserver"],
        )
    )


def test_health_answers_docker_inside_the_container(database_path):
    with TestClient(make_app(database_path), client=("127.0.0.1", 50000)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_hidden_from_everyone_else(database_path):
    with TestClient(make_app(database_path), client=("172.17.0.1", 50000)) as client:
        assert client.get("/health").status_code == 404
