from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.config import Settings
from app.main import create_app


@pytest_asyncio.fixture
async def client(tmp_path: Path):
    database_path = tmp_path / "test.db"
    settings = Settings(
        _env_file=None,
        app_env="test",
        dev_auth=True,
        db_auto_create=True,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        llm_mode="mock",
        mock_chunk_delay_seconds=0,
        sse_heartbeat_seconds=0.1,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
            yield test_client


@pytest.mark.asyncio
async def test_chat_lifecycle_and_stream(client: httpx.AsyncClient) -> None:
    me = await client.get("/api/v1/me", headers={"Authorization": "dev 42"})
    assert me.status_code == 200
    assert me.json()["telegram_user_id"] == 42

    created = await client.post(
        "/api/v1/chats",
        headers={"Authorization": "dev 42"},
        json={"title": "Проверка"},
    )
    assert created.status_code == 201
    chat_id = created.json()["id"]

    response = await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        headers={"Authorization": "dev 42", "Idempotency-Key": "message-1"},
        json={"content": "Привет"},
    )
    assert response.status_code == 200
    assert "event: message_start" in response.text
    assert "event: content_delta" in response.text
    assert "event: done" in response.text
    assert "mock-ответ" in response.text

    history = await client.get(
        f"/api/v1/chats/{chat_id}/messages", headers={"Authorization": "dev 42"}
    )
    assert history.status_code == 200
    messages = history.json()["items"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["status"] == "completed"
    assert "mock-ответ" in messages[1]["content"]

    duplicate = await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        headers={"Authorization": "dev 42", "Idempotency-Key": "message-1"},
        json={"content": "Привет ещё раз"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "duplicate_message"

    renamed = await client.patch(
        f"/api/v1/chats/{chat_id}",
        headers={"Authorization": "dev 42"},
        json={"title": "Новое название"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Новое название"

    deleted = await client.delete(
        f"/api/v1/chats/{chat_id}", headers={"Authorization": "dev 42"}
    )
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_chat_ownership_is_enforced(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/api/v1/chats",
        headers={"Authorization": "dev 1"},
        json={"title": "Private"},
    )
    chat_id = created.json()["id"]
    response = await client.get(
        f"/api/v1/chats/{chat_id}/messages", headers={"Authorization": "dev 2"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "chat_not_found"


@pytest.mark.asyncio
async def test_input_limit(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/api/v1/chats",
        headers={"Authorization": "dev 1"},
        json={"title": "Limits"},
    )
    chat_id = created.json()["id"]
    response = await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        headers={"Authorization": "dev 1"},
        json={"content": "x" * 16001},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "message_too_large"


def test_production_rejects_dev_auth() -> None:
    with pytest.raises(ValueError, match="DEV_AUTH"):
        Settings(_env_file=None, app_env="production", dev_auth=True)

