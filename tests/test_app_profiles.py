"""Tests for app/client profiles and their enforcement in the request path."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_model_router.apps.profiles import AppProfiles  # noqa: E402
from local_model_router.service.app import create_app  # noqa: E402

_FLEET_CONFIG = textwrap.dedent("""
    active_slots:
      - id: slot_router
        host: localhost
        port: 8080
        role: chat
        enabled: true
        router_mode: true
    global:
      backend: remote
""")

_APPS = textwrap.dedent("""
    apps:
      default:
        default_model: auto
        allowed_models: ["*"]
      aider:
        default_model: coder
        allowed_models: ["coder", "fast"]
      locked:
        default_model: chat
        allowed_models: ["chat"]
        allow_auto_route: false
""")


def _write(tmp_path, name, content):
    target = tmp_path / name
    target.write_text(content, encoding="utf-8")
    return str(target)


# ---------------------------------------------------------------------------
# profile policy (pure)
# ---------------------------------------------------------------------------

def test_profiles_load_and_lookup(tmp_path):
    profiles = AppProfiles.load(_write(tmp_path, "apps.yaml", _APPS))
    assert profiles.get("aider").default_model == "coder"
    assert profiles.get("AIDER").app_id == "aider"  # case-insensitive
    assert profiles.get("unknown").app_id == "default"
    assert profiles.get(None).app_id == "default"


def test_profiles_missing_file_is_permissive(tmp_path):
    profiles = AppProfiles.load(tmp_path / "missing.yaml")
    model, error = profiles.apply("anyone", "anything")
    assert (model, error) == ("anything", None)


def test_apply_defaults_and_restrictions(tmp_path):
    profiles = AppProfiles.load(_write(tmp_path, "apps.yaml", _APPS))

    # empty model -> profile default
    assert profiles.apply("aider", None) == ("coder", None)
    # allowed model passes
    assert profiles.apply("aider", "fast") == ("fast", None)
    # disallowed model rejected
    model, error = profiles.apply("aider", "deep")
    assert error == "model_not_allowed_for_app"
    # auto disabled
    model, error = profiles.apply("locked", "auto")
    assert error == "auto_route_disabled_for_app"


# ---------------------------------------------------------------------------
# HTTP enforcement
# ---------------------------------------------------------------------------

def _make_app(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    return create_app(
        _write(tmp_path, "llama_cpp_servers.yaml", _FLEET_CONFIG),
        apps_path=_write(tmp_path, "apps.yaml", _APPS),
        upstreams_path=str(tmp_path / "missing-upstreams.yaml"),
    )


def test_apps_endpoint_lists_profiles(tmp_path, monkeypatch):
    client = TestClient(_make_app(tmp_path, monkeypatch))
    resp = client.get("/apps")
    assert resp.status_code == 200
    by_id = {p["app_id"]: p for p in resp.json()["apps"]}
    assert by_id["aider"]["default_model"] == "coder"
    assert by_id["locked"]["allow_auto_route"] is False


def test_disallowed_model_rejected_with_403(tmp_path, monkeypatch):
    client = TestClient(_make_app(tmp_path, monkeypatch))
    resp = client.post(
        "/v1/chat/completions",
        headers={"X-App-Id": "aider"},
        json={"model": "deep", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 403
    payload = resp.json()
    assert payload["error"]["code"] == "model_not_allowed_for_app"
    assert payload["app_id"] == "aider"


def test_auto_rejected_when_profile_disables_it(tmp_path, monkeypatch):
    client = TestClient(_make_app(tmp_path, monkeypatch))
    resp = client.post(
        "/v1/chat/completions",
        headers={"X-App-Id": "locked"},
        json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "auto_route_disabled_for_app"


def test_unknown_app_falls_back_to_default_profile(tmp_path, monkeypatch):
    """An unknown app id must not be rejected — it gets the default profile.

    The request proceeds into routing (and fails on no healthy slot in this
    hermetic environment, which is fine — it must NOT be a 403).
    """
    client = TestClient(_make_app(tmp_path, monkeypatch))
    resp = client.post(
        "/v1/chat/completions",
        headers={"X-App-Id": "never-seen-before"},
        json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code != 403
