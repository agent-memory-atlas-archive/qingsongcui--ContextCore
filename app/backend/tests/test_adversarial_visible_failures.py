"""Native source problems and conversation linking failures must be visible."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import create_schema
from app.routers import manage


def test_foreign_config_is_ignored_and_native_source_failure_is_visible():
    create_schema()
    home = Path.home()
    foreign = home / ".cc-switch"
    foreign.mkdir()
    (foreign / "cc-switch.db").write_bytes(b"invalid database")
    api = FastAPI()
    api.include_router(manage.router)
    with TestClient(api) as client:
        assert client.post("/api/manage/sync-local", json={}).status_code == 404
        assert client.get("/api/manage/migrations/sources").status_code == 404
        url = "/api/manage/hosts/codex/native/preview"
        empty = client.post(url, json={"kind": "providers"})
        assert empty.status_code == 200
        assert empty.json()["candidates"] == []
        config = home / ".codex/config.toml"
        config.parent.mkdir(exist_ok=True)
        config.write_text("broken = [")
        broken = client.post(url, json={"kind": "providers"})
        assert broken.status_code >= 400 or broken.json()["status"] != empty.json()["status"]
        assert config.read_text() == "broken = ["


def test_unlinked_conversation_failure_is_visible(monkeypatch):
    from app.services import handoff_service

    class Result:
        def fetchall(self):
            return [{"id": "c1", "cwd": "/tmp/synthetic-unlinked", "title": "synthetic"}]

    class Conn:
        def execute(self, *_args, **_kwargs):
            return Result()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(handoff_service, "db", lambda: Conn())

    def boom(_path):
        raise RuntimeError("synthetic match failure")

    monkeypatch.setattr(handoff_service.project_service, "resolve_project_by_path", boom)
    assert handoff_service._link_unlinked_conversations_to_projects() == 1

    monkeypatch.setattr("app.services.connection_service.list_connections", lambda: [])
    synced = handoff_service.sync_recent_agent_chats_for_relay()
    assert any(item.get("status") == "error" for item in synced)
    assert all("synthetic match failure" not in str(item) for item in synced)
