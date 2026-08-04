import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from app.auth import TelegramAuthError, validate_init_data


def signed_init_data(bot_token: str, *, auth_date: int | None = None) -> str:
    values = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "test-query",
        "user": json.dumps(
            {
                "id": 123456789,
                "first_name": "Test",
                "username": "test_user",
                "language_code": "ru",
            },
            separators=(",", ":"),
        ),
    }
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_valid_init_data() -> None:
    identity = validate_init_data(signed_init_data("123:test-token"), "123:test-token", 3600)
    assert identity.user_id == 123456789
    assert identity.username == "test_user"
    assert identity.language_code == "ru"


def test_invalid_signature_is_rejected() -> None:
    init_data = signed_init_data("123:test-token").replace("test_user", "attacker")
    with pytest.raises(TelegramAuthError, match="signature"):
        validate_init_data(init_data, "123:test-token", 3600)


def test_expired_init_data_is_rejected() -> None:
    init_data = signed_init_data("123:test-token", auth_date=int(time.time()) - 7200)
    with pytest.raises(TelegramAuthError, match="expired"):
        validate_init_data(init_data, "123:test-token", 3600)

