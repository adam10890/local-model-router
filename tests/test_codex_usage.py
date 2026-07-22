from __future__ import annotations

import base64
import json

from local_model_router.helpers import codex_usage
from local_model_router.helpers.codex_usage import HttpResponse, fetch_codex_usage

_FRESH_ACCESS_CLAIMS = {"exp": 99999999999}  # far future, never triggers refresh


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _fake_jwt(claims: dict) -> str:
    """Build a JWT-shaped string (unsigned) for tests — never a real token."""
    return f"{_b64url({'alg': 'none'})}.{_b64url(claims)}.fakesig"


def _write_auth(tmp_path, tokens: dict) -> str:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"tokens": tokens}), encoding="utf-8")
    return str(auth_path)


def _unused_get(url, headers):
    raise AssertionError("http_get should not have been called")


def _unused_post(url, json_body):
    raise AssertionError("http_post should not have been called")


def test_happy_path_normalizes_primary_and_secondary_windows(tmp_path):
    access = _fake_jwt(_FRESH_ACCESS_CLAIMS)
    auth_path = _write_auth(
        tmp_path,
        {"access_token": access, "refresh_token": "refresh-abc", "account_id": "acct_direct"},
    )
    body = {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {"used_percent": 42.5, "window_minutes": 300, "resets_at": "2026-07-21T12:00:00Z"},
            "secondary_window": {"used_percent": 10, "window_seconds": 604800, "resets_at": "2026-07-28T00:00:00Z"},
        },
    }

    def http_get(url, headers):
        assert headers["Authorization"] == f"Bearer {access}"
        assert headers["ChatGPT-Account-Id"] == "acct_direct"
        return HttpResponse(200, {}, json.dumps(body).encode())

    result = fetch_codex_usage(auth_path=auth_path, http_get=http_get, http_post=_unused_post)

    assert result["available"] is True
    assert result["plan_type"] == "plus"
    windows = {w["label"]: w for w in result["windows"]}
    assert windows["5h"]["used_percent"] == 42.5
    assert windows["5h"]["remaining_percent"] == 57.5
    assert windows["7d"]["used_percent"] == 10
    assert windows["7d"]["resets_at"] == "2026-07-28T00:00:00Z"


def test_header_fallback_used_when_body_is_empty(tmp_path):
    access = _fake_jwt(_FRESH_ACCESS_CLAIMS)
    auth_path = _write_auth(tmp_path, {"access_token": access, "account_id": "acct_x"})
    headers = {
        "x-codex-primary-used-percent": "12.5",
        "x-codex-primary-window-minutes": "300",
        "x-codex-primary-resets-at": "2026-07-21T12:00:00Z",
        "x-codex-secondary-used-percent": "3",
        "x-codex-secondary-window-minutes": "10080",
        "x-codex-secondary-resets-at": "2026-07-28T00:00:00Z",
    }

    def http_get(url, req_headers):
        return HttpResponse(200, headers, b"")

    result = fetch_codex_usage(auth_path=auth_path, http_get=http_get, http_post=_unused_post)

    assert result["available"] is True
    labels = {w["label"]: w for w in result["windows"]}
    assert labels.keys() == {"5h", "7d"}
    assert labels["5h"]["used_percent"] == 12.5
    assert labels["7d"]["used_percent"] == 3.0


def test_account_id_derived_from_id_token_when_tokens_account_id_absent(tmp_path):
    access = _fake_jwt(_FRESH_ACCESS_CLAIMS)
    id_token = _fake_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct_from_jwt"}})
    auth_path = _write_auth(tmp_path, {"access_token": access, "id_token": id_token})
    seen = {}

    def http_get(url, headers):
        seen["account"] = headers["ChatGPT-Account-Id"]
        body = {"rate_limit": {"primary": {"used_percent": 1, "window_seconds": 18000}}}
        return HttpResponse(200, {}, json.dumps(body).encode())

    result = fetch_codex_usage(auth_path=auth_path, http_get=http_get, http_post=_unused_post)

    assert result["available"] is True
    assert seen["account"] == "acct_from_jwt"


def test_expired_access_token_is_refreshed_in_memory(tmp_path):
    expired = _fake_jwt({"exp": 1})
    auth_path = _write_auth(
        tmp_path, {"access_token": expired, "refresh_token": "refresh-xyz", "account_id": "acct_x"}
    )
    new_access = "new-access-token-value"

    def http_post(url, json_body):
        assert url == codex_usage._TOKEN_URL
        assert json_body["refresh_token"] == "refresh-xyz"
        assert json_body["client_id"] == codex_usage._CLIENT_ID
        return HttpResponse(200, {}, json.dumps({"access_token": new_access}).encode())

    seen = {}

    def http_get(url, headers):
        seen["auth_header"] = headers["Authorization"]
        body = {"rate_limit": {"primary": {"used_percent": 5, "window_seconds": 18000}}}
        return HttpResponse(200, {}, json.dumps(body).encode())

    result = fetch_codex_usage(auth_path=auth_path, http_get=http_get, http_post=http_post)

    assert result["available"] is True
    assert seen["auth_header"] == f"Bearer {new_access}"


def test_refresh_failure_is_unavailable(tmp_path):
    expired = _fake_jwt({"exp": 1})
    auth_path = _write_auth(
        tmp_path, {"access_token": expired, "refresh_token": "refresh-xyz", "account_id": "acct_x"}
    )

    def http_post(url, json_body):
        return HttpResponse(401, {}, b'{"error":"invalid_grant"}')

    result = fetch_codex_usage(auth_path=auth_path, http_get=_unused_get, http_post=http_post)

    assert result == {"available": False, "reason": "token refresh failed", "windows": []}


def test_missing_auth_file_is_unavailable(tmp_path):
    missing = tmp_path / "nope.json"

    result = fetch_codex_usage(auth_path=str(missing), http_get=_unused_get, http_post=_unused_post)

    assert result["available"] is False
    assert result["windows"] == []


def test_malformed_auth_json_is_unavailable(tmp_path):
    bad = tmp_path / "auth.json"
    bad.write_text("{not valid json", encoding="utf-8")

    result = fetch_codex_usage(auth_path=str(bad), http_get=_unused_get, http_post=_unused_post)

    assert result["available"] is False


def test_non_200_usage_response_on_all_fallbacks_is_unavailable(tmp_path):
    access = _fake_jwt(_FRESH_ACCESS_CLAIMS)
    auth_path = _write_auth(tmp_path, {"access_token": access, "account_id": "acct_x"})

    def http_get(url, headers):
        return HttpResponse(500, {}, b"server error")

    result = fetch_codex_usage(auth_path=auth_path, http_get=http_get, http_post=_unused_post)

    assert result["available"] is False
    assert access not in json.dumps(result)


def test_network_error_is_unavailable_never_raises(tmp_path):
    access = _fake_jwt(_FRESH_ACCESS_CLAIMS)
    auth_path = _write_auth(tmp_path, {"access_token": access, "account_id": "acct_x"})

    def http_get(url, headers):
        raise OSError("network down")

    result = fetch_codex_usage(auth_path=auth_path, http_get=http_get, http_post=_unused_post)

    assert result == {"available": False, "reason": "usage endpoint unavailable", "windows": []}


def test_codex_home_env_var_used_when_auth_path_not_given(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    access = _fake_jwt(_FRESH_ACCESS_CLAIMS)
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": access, "account_id": "acct_x"}}), encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def http_get(url, headers):
        body = {"rate_limit": {"primary": {"used_percent": 1, "window_seconds": 18000}}}
        return HttpResponse(200, {}, json.dumps(body).encode())

    result = fetch_codex_usage(http_get=http_get, http_post=_unused_post)

    assert result["available"] is True


def test_tokens_never_appear_in_returned_dict(tmp_path):
    access = _fake_jwt(_FRESH_ACCESS_CLAIMS)
    id_token = _fake_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct_x"}})
    refresh = "super-secret-refresh-token-value"
    auth_path = _write_auth(
        tmp_path, {"access_token": access, "refresh_token": refresh, "id_token": id_token}
    )

    def http_get(url, headers):
        body = {"rate_limit": {"primary": {"used_percent": 1, "window_seconds": 18000}}}
        return HttpResponse(200, {}, json.dumps(body).encode())

    result = fetch_codex_usage(auth_path=auth_path, http_get=http_get, http_post=_unused_post)

    dumped = json.dumps(result)
    assert access not in dumped
    assert refresh not in dumped
    assert id_token not in dumped
