"""Hermetic tests for the router-backed built-in agent library."""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import aiohttp
from starlette.testclient import TestClient

from local_model_router.service.agent_library import (
    AGENT_INPUT_MAX_BYTES,
    AgentCatalog,
    AgentDefinition,
    AgentRunTimeout,
    model_settings,
    run_agent,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_AGENTS_PATH = REPO_ROOT / "conf" / "agents.yaml"
_EMPTY_FLEET = "active_slots: []\nglobal:\n  backend: remote\n"
_UPSTREAM_CONFIG = """\
upstreams:
  - name: fake
    type: openai_compatible
    base_url: http://fake.invalid/v1
    enabled: true
    capabilities: [chat]
    models: [fake-chat]
"""


def _client(tmp_path, monkeypatch, *, api_key: str = ""):
    from local_model_router.helpers.llama_cpp_manager import BackendManager
    from local_model_router.service.app import create_app
    from local_model_router.service.fleet_manager import FleetStore

    BackendManager._instance = None
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(_EMPTY_FLEET, encoding="utf-8")
    if api_key:
        monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", api_key)
    else:
        monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    return TestClient(
        create_app(
            str(cfg),
            agents_path=str(_AGENTS_PATH),
            fleet_store=FleetStore(":memory:"),
        )
    ), BackendManager


def test_catalog_exposes_builtins_without_prompts():
    catalog = AgentCatalog.load(_AGENTS_PATH)

    public = catalog.public_list()

    assert {agent["id"] for agent in public} == {
        "code-review",
        "implementation-plan",
        "test-design",
        "documentation-writer",
    }
    assert all("system_prompt" not in agent for agent in public)
    assert catalog.get("code-review").routing.routing_strategy == "quality"


def test_windows_scripts_prepare_and_stop_the_agent_runner():
    setup = (REPO_ROOT / "SETUP.bat").read_text(encoding="utf-8")
    start = (REPO_ROOT / "START.bat").read_text(encoding="utf-8")
    stop = (REPO_ROOT / "STOP.bat").read_text(encoding="utf-8")

    assert '.[dev,mcp,agents]' in setup
    assert 'A0_LMM_ROUTER_AGENT_BASE_URL=%BASE_URL%/v1' in start
    assert 'for %%P in (%OBSERVER_PORT% 8089)' in stop


def test_runner_settings_propagate_routing_and_local_only():
    definition = AgentDefinition.model_validate(
        {
            "id": "private-review",
            "name": "Private Review",
            "description": "Review private input.",
            "system_prompt": "Review supplied input.",
            "routing": {
                "role": "chat",
                "task_type": "coding",
                "routing_strategy": "quality",
                "local_only": True,
            },
        }
    )

    settings = model_settings(definition)

    assert settings["extra_headers"] == {"X-App-Id": "agent_library"}
    assert settings["extra_body"]["routing"] == {
        "agent_id": "private-review",
        "agent_type": "custom",
        "role": "chat",
        "task_type": "coding",
        "routing_strategy": "quality",
        "local_only": True,
    }


def test_run_agent_uses_chat_completions_model_and_router_metadata(monkeypatch):
    captured = {}

    class FakeProvider:
        def __init__(self, **kwargs):
            captured["provider"] = kwargs

    class FakeModel:
        def __init__(self, model_name, *, provider):
            captured["model_name"] = model_name
            captured["model_provider"] = provider

    class FakeAgent:
        def __init__(self, model, *, system_prompt):
            captured["agent_model"] = model
            captured["system_prompt"] = system_prompt

        async def run(self, user_input, *, model_settings):
            captured["input"] = user_input
            captured["settings"] = model_settings
            return types.SimpleNamespace(output="review complete")

    pydantic_ai = types.ModuleType("pydantic_ai")
    pydantic_ai.__path__ = []
    pydantic_ai.Agent = FakeAgent
    models = types.ModuleType("pydantic_ai.models")
    models.__path__ = []
    openai_models = types.ModuleType("pydantic_ai.models.openai")
    openai_models.OpenAIChatModel = FakeModel
    providers = types.ModuleType("pydantic_ai.providers")
    providers.__path__ = []
    openai_providers = types.ModuleType("pydantic_ai.providers.openai")
    openai_providers.OpenAIProvider = FakeProvider
    monkeypatch.setitem(sys.modules, "pydantic_ai", pydantic_ai)
    monkeypatch.setitem(sys.modules, "pydantic_ai.models", models)
    monkeypatch.setitem(sys.modules, "pydantic_ai.models.openai", openai_models)
    monkeypatch.setitem(sys.modules, "pydantic_ai.providers", providers)
    monkeypatch.setitem(sys.modules, "pydantic_ai.providers.openai", openai_providers)
    monkeypatch.setenv("A0_LMM_ROUTER_AGENT_BASE_URL", "http://127.0.0.1:9001/v1")
    monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", "test-key")
    definition = AgentCatalog.load(_AGENTS_PATH).get("code-review")

    output = asyncio.run(run_agent(definition, "Review this."))

    assert output == "review complete"
    assert captured["model_name"] == "auto"
    assert captured["provider"] == {
        "base_url": "http://127.0.0.1:9001/v1",
        "api_key": "test-key",
    }
    assert captured["input"] == "Review this."
    assert captured["settings"]["extra_headers"] == {"X-App-Id": "agent_library"}
    assert captured["settings"]["extra_body"]["routing"]["agent_id"] == "code-review"


def test_agent_run_validates_input_and_reports_unavailable_runner(tmp_path, monkeypatch):
    client, manager_cls = _client(tmp_path, monkeypatch)
    monkeypatch.delenv("A0_LMM_ROUTER_AGENT_BASE_URL", raising=False)

    empty = client.post("/agents/code-review/runs", json={"input": "  "})
    too_large = client.post(
        "/agents/code-review/runs",
        json={"input": "x" * (AGENT_INPUT_MAX_BYTES + 1)},
    )
    unavailable = client.post("/agents/code-review/runs", json={"input": "Review this."})

    assert empty.status_code == 400
    assert empty.json()["error"] == "invalid_agent_input"
    assert too_large.status_code == 413
    assert too_large.json()["error"] == "input_too_large"
    assert unavailable.status_code == 503
    assert unavailable.json()["error"] == "agent_runner_unavailable"
    manager_cls._instance = None


def test_agent_run_uses_catalog_definition_and_hides_prompt(tmp_path, monkeypatch):
    import local_model_router.service.app as app_module

    client, manager_cls = _client(tmp_path, monkeypatch)
    observed = {}

    async def fake_run(definition, user_input):
        observed["id"] = definition.id
        observed["input"] = user_input
        observed["prompt"] = definition.system_prompt
        return "- Finding: missing test"

    monkeypatch.setattr(app_module, "run_agent", fake_run)

    response = client.post("/agents/code-review/runs", json={"input": "Review this diff."})

    assert response.status_code == 200
    assert response.json() == {"agent_id": "code-review", "output": "- Finding: missing test"}
    assert observed["id"] == "code-review"
    assert observed["input"] == "Review this diff."
    assert "Review only" in observed["prompt"]
    assert "system_prompt" not in str(response.json())
    manager_cls._instance = None


def test_agent_run_maps_timeout_and_requires_auth(tmp_path, monkeypatch):
    import local_model_router.service.app as app_module

    client, manager_cls = _client(tmp_path, monkeypatch, api_key="local-secret")

    async def fake_timeout(definition, user_input):
        raise AgentRunTimeout()

    monkeypatch.setattr(app_module, "run_agent", fake_timeout)

    unauthorized = client.get("/agents")
    timed_out = client.post(
        "/agents/code-review/runs",
        headers={"Authorization": "Bearer local-secret"},
        json={"input": "Review this."},
    )

    assert unauthorized.status_code == 401
    assert timed_out.status_code == 504
    assert timed_out.json()["error"] == "agent_timeout"
    manager_cls._instance = None


def test_agent_routing_metadata_is_distinguishable_when_auto_upstream_forwards(tmp_path, monkeypatch):
    from local_model_router.helpers.llama_cpp_manager import BackendManager
    from local_model_router.service.app import create_app
    from local_model_router.service.fleet_manager import FleetStore

    BackendManager._instance = None
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(_EMPTY_FLEET, encoding="utf-8")
    upstreams = tmp_path / "upstreams.yaml"
    upstreams.write_text(_UPSTREAM_CONFIG, encoding="utf-8")
    store = FleetStore(":memory:")
    monkeypatch.setenv("A0_LMM_ROUTER_AUTO_UPSTREAMS", "1")

    calls = []

    class FakeResponse:
        status = 200

        async def json(self, content_type=None):
            return {
                "id": "chatcmpl-agent",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            }

        async def text(self):
            return "not-json"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def post(self, url, **kwargs):
            calls.append({"url": url, "kwargs": kwargs})

            class Context:
                async def __aenter__(self):
                    return FakeResponse()

                async def __aexit__(self, *args):
                    return None

            return Context()

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    client = TestClient(
        create_app(
            str(cfg),
            upstreams_path=str(upstreams),
            agents_path=str(_AGENTS_PATH),
            fleet_store=store,
        )
    )
    catalog = AgentCatalog.load(_AGENTS_PATH)
    settings = model_settings(catalog.get("code-review"))
    payload = {
        "model": "auto",
        "messages": [{"role": "user", "content": "Review this."}],
        **settings["extra_body"],
    }

    forwarded = client.post("/v1/chat/completions", headers=settings["extra_headers"], json=payload)
    analytics = client.get("/routing/analytics").json()

    assert forwarded.status_code == 200
    assert calls[0]["url"] == "http://fake.invalid/v1/chat/completions"
    assert analytics["recent"][0]["status"] == "forwarded_upstream"
    assert analytics["recent"][0]["app_id"] == "agent_library"
    assert analytics["recent"][0]["agent_id"] == "code-review"

    local_only_definition = AgentDefinition.model_validate(
        {
            "id": "local-review",
            "name": "Local Review",
            "description": "Keep review local.",
            "system_prompt": "Review supplied input.",
            "routing": {"role": "chat", "task_type": "coding", "local_only": True},
        }
    )
    local_only_settings = model_settings(local_only_definition)
    blocked = client.post(
        "/v1/chat/completions",
        headers=local_only_settings["extra_headers"],
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "Keep this private."}],
            **local_only_settings["extra_body"],
        },
    )

    assert blocked.status_code == 503
    assert len(calls) == 1
    BackendManager._instance = None
