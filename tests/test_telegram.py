import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest
import requests

from tourfinder.telegram_bot import BotAPIError, BotClient, InitDataError, validate_init_data

TOKEN = "123:test-only-not-a-real-token"


def signed(user_id=123, stamp=1000, **extra):
    values = {"auth_date": str(stamp), "user": json.dumps({"id": user_id, "first_name": "Test"}), **extra}
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, "\n".join(f"{k}={v}" for k, v in sorted(values.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_signed_data_including_signature():
    assert validate_init_data(signed(signature="additional-signed-field"), TOKEN, now=1000)["id"] == 123


@pytest.mark.parametrize("raw", [signed() + "&auth_date=1000", signed().replace("1000", "1001"),
                                 signed(stamp=1), signed(stamp=5000), signed(user_id=True),
                                 signed(user_id="123"), signed(user_id=-1), "hash=bad"])
def test_reject_untrusted_expired_or_malformed_sessions(raw):
    with pytest.raises(InitDataError):
        validate_init_data(raw, TOKEN, now=4000)


def test_wrong_token():
    with pytest.raises(InitDataError):
        validate_init_data(signed(), "other", now=1000)


def test_timeout_is_ambiguous_and_never_leaks_token(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.Timeout(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    monkeypatch.setattr(requests, "post", fail)
    with pytest.raises(BotAPIError) as captured:
        BotClient(TOKEN).call("sendMessage", chat_id=123, text="test")
    assert captured.value.ambiguous
    assert TOKEN not in str(captured.value)


def test_429_retains_retry_after(monkeypatch):
    class Response:
        status_code = 429
        def json(self):
            return {"ok": False, "error_code": 429, "parameters": {"retry_after": 12}}
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response())
    with pytest.raises(BotAPIError) as captured:
        BotClient(TOKEN).call("sendMessage")
    assert captured.value.code == 429
    assert captured.value.retry_after == 12
    assert not captured.value.ambiguous
