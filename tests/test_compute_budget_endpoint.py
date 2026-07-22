"""Tests for the /compute/budget HTTP endpoint."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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

_UPSTREAMS = textwrap.dedent("""
    upstreams:
      - name: capped
        type: openai_compatible
        base_url: http://localhost:9999/v1
        enabled: true
        limits:
          - window: "1d"
            max_tokens: 1000000
""")


def _write(tmp_path, name, content):
    target = tmp_path / name
    target.write_text(content, encoding="utf-8")
    return str(target)


def _make_app(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    return create_app(
        _write(tmp_path, "llama_cpp_servers.yaml", _FLEET_CONFIG),
        upstreams_path=_write(tmp_path, "upstreams.yaml", _UPSTREAMS),
        apps_path=str(tmp_path / "missing-apps.yaml"),
    )


def test_compute_budget_endpoint_returns_local_and_provider_state(tmp_path, monkeypatch):
    client = TestClient(_make_app(tmp_path, monkeypatch))

    resp = client.get("/compute/budget")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"ts", "local", "providers"}
    providers = {p["provider"]: p for p in body["providers"]}
    assert "capped" in providers
    assert "status" in providers["capped"]
