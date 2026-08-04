import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .db import get_session
from .models import User


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None


class TelegramAuthError(ValueError):
    pass


def validate_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int,
    future_skew_seconds: int = 30,
    now: int | None = None,
) -> TelegramIdentity:
    if not init_data or not bot_token:
        raise TelegramAuthError("Telegram authentication is not configured")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise TelegramAuthError("initData hash is missing")

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise TelegramAuthError("initData signature is invalid")

    current_time = int(time.time()) if now is None else now
    try:
        auth_date = int(pairs["auth_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TelegramAuthError("initData auth_date is invalid") from exc

    age = current_time - auth_date
    if age > max_age_seconds:
        raise TelegramAuthError("initData has expired")
    if age < -future_skew_seconds:
        raise TelegramAuthError("initData auth_date is in the future")

    try:
        payload = json.loads(pairs["user"])
        user_id = int(payload["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TelegramAuthError("initData user is invalid") from exc

    return TelegramIdentity(
        user_id=user_id,
        username=payload.get("username"),
        first_name=payload.get("first_name"),
        last_name=payload.get("last_name"),
        language_code=payload.get("language_code"),
    )


def _authenticate_request(authorization: str | None, settings: Settings) -> TelegramIdentity:
    if settings.dev_auth and settings.app_env != "production":
        if not authorization:
            return TelegramIdentity(
                user_id=settings.dev_telegram_user_id,
                username="local_user",
                first_name="Local",
            )
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "dev":
            try:
                user_id = int(value or settings.dev_telegram_user_id)
            except ValueError as exc:
                raise TelegramAuthError("Development user ID is invalid") from exc
            return TelegramIdentity(
                user_id=user_id,
                username=f"local_{user_id}",
                first_name="Local",
            )

    if not authorization:
        raise TelegramAuthError("Authorization header is missing")
    scheme, separator, init_data = authorization.partition(" ")
    if not separator or scheme.lower() != "tma":
        raise TelegramAuthError("Authorization scheme must be tma")

    return validate_init_data(
        init_data=init_data,
        bot_token=settings.telegram_bot_token,
        max_age_seconds=settings.telegram_init_data_max_age_seconds,
        future_skew_seconds=settings.telegram_init_data_future_skew_seconds,
    )


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User:
    settings: Settings = request.app.state.settings
    try:
        identity = _authenticate_request(authorization, settings)
    except (TelegramAuthError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "telegram_auth_failed", "message": str(exc)},
        ) from exc

    user = await session.get(User, identity.user_id)
    if user is None:
        user = User(telegram_user_id=identity.user_id)
        session.add(user)

    user.username = identity.username
    user.first_name = identity.first_name
    user.last_name = identity.last_name
    user.language_code = identity.language_code
    user.last_seen_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(user)
    return user
