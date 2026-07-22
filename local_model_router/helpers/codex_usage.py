"""Live Codex/ChatGPT usage reader.

Reads the Codex CLI's own OAuth credentials (``~/.codex/auth.json``) and asks
ChatGPT's backend for the current 5h/7d rate-limit window usage — the same
numbers the ``codex`` CLI shows itself. This is the *live* budget source for
``subscription``-type upstreams (see ``local_model_router/upstreams/registry.py``);
declared ``limits:`` entries in ``upstreams.yaml`` are only a fallback for when
this read is unavailable.

Percent-used + reset time only, never absolute token counts — Codex doesn't
expose those, and the router doesn't need them to throttle.

Security: this reads (never writes) ``auth.json``, and refreshes an expired
access token in memory only. The access/refresh/id tokens must never appear
in a log, an exception, or the returned dict — every failure mode (missing
file, bad json, expired token with no refresh, network error, non-200)
degrades to ``{"available": False, "reason": <short non-sensitive string>}``
rather than raising.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, NamedTuple, Optional

_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_USAGE_URLS = (
    "https://chatgpt.com/backend-api/codex/usage",
    "https://chatgpt.com/backend-api/wham/usage",
    "https://chatgpt.com/api/codex/usage",
)
_REFRESH_SKEW_SECONDS = 60


class HttpResponse(NamedTuple):
    status_code: int
    headers: Dict[str, str]
    body: bytes


def _default_http_get(url: str, headers: Dict[str, str]) -> HttpResponse:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return HttpResponse(resp.status, dict(resp.headers), resp.read())
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, dict(exc.headers or {}), exc.read() or b"")


def _default_http_post(url: str, json_body: Dict[str, Any]) -> HttpResponse:
    data = json.dumps(json_body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return HttpResponse(resp.status, dict(resp.headers), resp.read())
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, dict(exc.headers or {}), exc.read() or b"")


def _parse_jwt_claims(jwt: str) -> Dict[str, Any]:
    """Decode a JWT's claims without verifying its signature (client-side read only)."""
    try:
        payload = jwt.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception:
        return {}


def _unavailable(reason: str) -> Dict[str, Any]:
    return {"available": False, "reason": reason, "windows": []}


def _resolve_auth_path(auth_path: Optional[str]) -> Path:
    if auth_path:
        return Path(auth_path).expanduser()
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home) if codex_home else Path("~/.codex")
    return base.expanduser() / "auth.json"


def _refresh_access_token(refresh_token: str, http_post: Callable[[str, Dict[str, Any]], HttpResponse]) -> Optional[str]:
    try:
        resp = http_post(
            _TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": _CLIENT_ID,
                "refresh_token": refresh_token,
            },
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        new_token = json.loads(resp.body).get("access_token")
    except Exception:
        return None
    return new_token if isinstance(new_token, str) and new_token else None


def _label_for(window_seconds: Optional[float]) -> str:
    if not window_seconds:
        return "?"
    seconds = float(window_seconds)
    if abs(seconds - 18000) < 60:
        return "5h"
    if abs(seconds - 604800) < 60:
        return "7d"
    if seconds % 86400 == 0:
        return f"{int(seconds // 86400)}d"
    if seconds % 3600 == 0:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 60)}m"


def _build_window(used_percent: Any, window_seconds: Any, resets_at: Any) -> Optional[Dict[str, Any]]:
    try:
        used = float(used_percent)
    except (TypeError, ValueError):
        return None
    seconds = None
    if window_seconds is not None:
        try:
            seconds = float(window_seconds)
        except (TypeError, ValueError):
            seconds = None
    return {
        "label": _label_for(seconds),
        "used_percent": used,
        "remaining_percent": max(0.0, 100.0 - used),
        "window_seconds": seconds,
        "resets_at": resets_at,
    }


def _normalize_window_body(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    used_percent = raw.get("used_percent", raw.get("usedPercent"))
    if used_percent is None:
        return None
    window_seconds = raw.get("window_seconds", raw.get("windowSeconds"))
    if window_seconds is None:
        window_minutes = raw.get("window_minutes", raw.get("windowMinutes"))
        if window_minutes is not None:
            try:
                window_seconds = float(window_minutes) * 60
            except (TypeError, ValueError):
                window_seconds = None
    resets_at = raw.get("resets_at", raw.get("reset_at", raw.get("resetsAt")))
    return _build_window(used_percent, window_seconds, resets_at)


def _windows_from_body(body_json: Dict[str, Any]) -> list:
    rate_limit = body_json.get("rate_limit") or body_json.get("rateLimit") or {}
    windows = []
    for primary_key, secondary_key in (("primary_window", "secondary_window"), ("primary", "secondary")):
        primary_raw = rate_limit.get(primary_key)
        secondary_raw = rate_limit.get(secondary_key)
        if primary_raw or secondary_raw:
            for raw in (primary_raw, secondary_raw):
                w = _normalize_window_body(raw) if raw else None
                if w:
                    windows.append(w)
            break
    return windows


def _windows_from_headers(headers: Dict[str, str]) -> list:
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    windows = []
    for which in ("primary", "secondary"):
        used = lowered.get(f"x-codex-{which}-used-percent")
        if used is None:
            continue
        window_minutes = lowered.get(f"x-codex-{which}-window-minutes")
        resets_at = lowered.get(f"x-codex-{which}-resets-at")
        window_seconds = None
        if window_minutes is not None:
            try:
                window_seconds = float(window_minutes) * 60
            except (TypeError, ValueError):
                window_seconds = None
        w = _build_window(used, window_seconds, resets_at)
        if w:
            windows.append(w)
    return windows


def fetch_codex_usage(
    *,
    auth_path: Optional[str] = None,
    http_get: Callable[[str, Dict[str, str]], HttpResponse] = _default_http_get,
    http_post: Callable[[str, Dict[str, Any]], HttpResponse] = _default_http_post,
    now: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Fetch live Codex/ChatGPT rate-limit usage. Never raises.

    Returns ``{"available": True, "reason": "", "plan_type": str, "windows": [...]}``
    on success, each window being
    ``{"label", "used_percent", "remaining_percent", "window_seconds", "resets_at"}``.
    On any failure: ``{"available": False, "reason": "<short reason>", "windows": []}``.
    """
    try:
        return _fetch_codex_usage(auth_path, http_get, http_post, now)
    except Exception:
        return _unavailable("unexpected error reading codex usage")


def _fetch_codex_usage(
    auth_path: Optional[str],
    http_get: Callable[[str, Dict[str, str]], HttpResponse],
    http_post: Callable[[str, Dict[str, Any]], HttpResponse],
    now: Callable[[], float],
) -> Dict[str, Any]:
    path = _resolve_auth_path(auth_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _unavailable("auth file missing or unreadable")

    tokens = data.get("tokens") or {}
    access_token = tokens.get("access_token")
    if not access_token:
        return _unavailable("no access token in auth file")
    refresh_token = tokens.get("refresh_token")
    id_token = tokens.get("id_token")

    account_id = tokens.get("account_id")
    if not account_id and id_token:
        claims = _parse_jwt_claims(id_token)
        account_id = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
    if not account_id:
        return _unavailable("no account id available")

    claims = _parse_jwt_claims(access_token)
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp - now() < _REFRESH_SKEW_SECONDS:
        if not refresh_token:
            return _unavailable("access token expired and no refresh token")
        refreshed = _refresh_access_token(refresh_token, http_post)
        if not refreshed:
            return _unavailable("token refresh failed")
        access_token = refreshed

    headers = {
        "Authorization": f"Bearer {access_token}",
        "ChatGPT-Account-Id": account_id,
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    resp = None
    for url in _USAGE_URLS:
        try:
            candidate = http_get(url, headers)
        except Exception:
            continue
        if candidate.status_code == 200:
            resp = candidate
            break
    if resp is None:
        return _unavailable("usage endpoint unavailable")

    try:
        body_json = json.loads(resp.body) if resp.body else {}
    except (ValueError, TypeError):
        body_json = {}

    windows = _windows_from_body(body_json) if body_json else []
    if not windows:
        windows = _windows_from_headers(resp.headers)
    if not windows:
        return _unavailable("usage response had no rate-limit windows")

    plan_type = body_json.get("plan_type") or body_json.get("planType") or ""
    return {"available": True, "reason": "", "plan_type": plan_type, "windows": windows}
