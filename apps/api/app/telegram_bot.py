import hmac
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, status

from .config import Settings

router = APIRouter()


async def _telegram_call(
    settings: Settings, method: str, payload: dict[str, Any]
) -> dict[str, Any]:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram Bot API method {method} failed")
    return data


async def send_start_message(settings: Settings, chat_id: int) -> None:
    await _telegram_call(
        settings,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "Откройте приложение, чтобы начать чат с моделью.",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "Открыть чат", "web_app": {"url": settings.mini_app_url}}]
                ]
            },
        },
    )


async def configure_telegram(settings: Settings) -> None:
    if not settings.telegram_bot_token or not settings.telegram_webhook_secret:
        raise RuntimeError("Telegram token and webhook secret are required")
    webhook_url = f"{settings.public_base_url.rstrip('/')}/telegram/webhook"
    await _telegram_call(
        settings,
        "setWebhook",
        {
            "url": webhook_url,
            "secret_token": settings.telegram_webhook_secret,
            "allowed_updates": ["message"],
            "drop_pending_updates": False,
        },
    )
    await _telegram_call(
        settings,
        "setChatMenuButton",
        {
            "menu_button": {
                "type": "web_app",
                "text": "Открыть чат",
                "web_app": {"url": settings.mini_app_url},
            }
        },
    )


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    settings: Settings = request.app.state.settings
    expected = settings.telegram_webhook_secret
    if not expected or not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        expected, x_telegram_bot_api_secret_token
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid secret")

    update = await request.json()
    message = update.get("message") or {}
    text = message.get("text") or ""
    chat = message.get("chat") or {}
    if text.split(maxsplit=1)[0] == "/start" and chat.get("id") is not None:
        await send_start_message(settings, int(chat["id"]))
    return {"ok": True}
