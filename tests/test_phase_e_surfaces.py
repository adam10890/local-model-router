"""Tests for Phase E surfaces: dashboard, A2A card + skills, MCP smoke."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
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


def _make_app(tmp_path, monkeypatch, api_key=None, **kwargs):
    if api_key:
        monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", api_key)
    else:
        monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    config = tmp_path / "llama_cpp_servers.yaml"
    config.write_text(_FLEET_CONFIG, encoding="utf-8")
    kwargs.setdefault("upstreams_path", str(tmp_path / "no-upstreams.yaml"))
    kwargs.setdefault("apps_path", str(tmp_path / "no-apps.yaml"))
    return create_app(str(config), **kwargs)


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------

def test_dashboard_page_loads(tmp_path, monkeypatch):
    client = TestClient(_make_app(tmp_path, monkeypatch))
    resp = client.get("/ui")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    html = resp.text
    assert "local-model-router" in html
    assert "Routing test panel" in html
    assert "/v1/models" in html  # the page consumes the router's own API
    assert 'get("/harnesses")' in html
    assert "Add harness" in html
    assert "Copy setup" in html
    assert "Verify" in html
    assert "@click=\"openTab('connect')\"" not in html
    assert 'data-dashboard-section="harnesses"' in html
    assert '<details class="harness-row"' in html
    assert "Connection guides" in html


# ---------------------------------------------------------------------------
# A2A
# ---------------------------------------------------------------------------

def test_agent_card_is_public_and_safe(tmp_path, monkeypatch):
    client = TestClient(_make_app(tmp_path, monkeypatch, api_key="sekret"))

    resp = client.get("/.well-known/agent-card.json")  # no key on purpose
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "Local Model Router Agent"
    assert {s["id"] for s in card["skills"]} == {
        "route_llm_task", "check_backend_health", "list_models",
    }
    assert card["metadata"]["local_only"] is True
    assert "sekret" not in resp.text


def test_a2a_skills_require_key_when_configured(tmp_path, monkeypatch):
    client = TestClient(_make_app(tmp_path, monkeypatch, api_key="sekret"))
    resp = client.post("/a2a", json={"skill": "list_models", "input": {}})
    assert resp.status_code == 401
    ok = client.post(
        "/a2a",
        headers={"Authorization": "Bearer sekret"},
        json={"skill": "check_backend_health", "input": {}},
    )
    assert ok.status_code == 200


def test_a2a_unknown_skill_is_404(tmp_path, monkeypatch):
    client = TestClient(_make_app(tmp_path, monkeypatch))
    resp = client.post("/a2a", json={"skill": "make_coffee", "input": {}})
    assert resp.status_code == 404
    assert "route_llm_task" in resp.json()["known_skills"]


def test_a2a_list_models_skill(tmp_path, monkeypatch):
    async def fake_fetch(base_url):
        return [{"id": "chat", "meta": {"n_ctx": 131072}}]

    client = TestClient(_make_app(tmp_path, monkeypatch, models_fetch=fake_fetch))
    resp = client.post("/a2a", json={"skill": "list_models", "input": {}})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["skill"] == "list_models"
    ids = {row["id"] for row in payload["result"]["data"]}
    assert "auto" in ids


def test_a2a_route_llm_task_returns_explainable_decision(tmp_path, monkeypatch):
    client = TestClient(_make_app(tmp_path, monkeypatch))
    resp = client.post(
        "/a2a",
        json={"skill": "route_llm_task", "input": {"task_type": "coding", "local_only": True}},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["role"] == "utility"
    assert result["local_only_enforced"] is True
    assert isinstance(result["reason_codes"], list)


# ---------------------------------------------------------------------------
# MCP (smoke — requires the optional mcp extra)
# ---------------------------------------------------------------------------

def test_mcp_app_factory_registers_tools():
    pytest.importorskip("mcp", reason="mcp extra not installed")
    import asyncio

    from local_model_router.mcp.server import create_app as create_mcp_app

    mcp_app = create_mcp_app(enable_auth=False, allow_mutating_tools=False)
    tools = asyncio.run(mcp_app.list_tools())
    names = {tool.name for tool in tools}
    assert {"chat_completion", "route_completion", "fleet_status", "list_slots"} <= names
    # mutating tools stay hidden unless explicitly enabled
    assert "start_fleet" not in names

    mcp_admin = create_mcp_app(enable_auth=False, allow_mutating_tools=True)
    admin_names = {tool.name for tool in asyncio.run(mcp_admin.list_tools())}
    assert "start_fleet" in admin_names


def test_mcp_token_paths_are_explicit(monkeypatch):
    pytest.importorskip("mcp", reason="mcp extra not installed")
    from local_model_router.mcp.server import _token_candidate_paths

    monkeypatch.delenv("MCP_TOKEN_PATH", raising=False)
    assert _token_candidate_paths() == []
    monkeypatch.setenv("MCP_TOKEN_PATH", "C:/secure/router.key")
    assert _token_candidate_paths() == ["C:/secure/router.key"]
