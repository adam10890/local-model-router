from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from local_model_router.mcp import router_bridge


ROOT = Path(__file__).resolve().parents[1]


class _Response:
    def __init__(self, status: int = 200, payload=None, json_error: Exception | None = None):
        self.status = status
        self.payload = {"ok": True} if payload is None else payload
        self.json_error = json_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, **_kwargs):
        if self.json_error:
            raise self.json_error
        return self.payload


class _Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            return _RaisingContext(response)
        return response


class _RaisingContext:
    def __init__(self, error: Exception):
        self.error = error

    async def __aenter__(self):
        raise self.error

    async def __aexit__(self, *_args):
        return None


class _Registrar:
    def __init__(self):
        self.tools = {}
        self.resources = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return register

    def resource(self, uri):
        def register(fn):
            self.resources[uri] = fn
            return fn

        return register


def _use_session(monkeypatch, *responses):
    session = _Session(responses)
    monkeypatch.setattr(router_bridge.aiohttp, "ClientSession", lambda: session)
    return session


def test_bridge_sends_exact_router_paths_auth_and_json(monkeypatch):
    monkeypatch.setenv("A0_LMM_ROUTER_BASE_URL", "http://router.test:9100/")
    monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", "secret-key")
    monkeypatch.setenv("A0_MCP_AGENT_ID", "mcp-test")
    monkeypatch.setenv("A0_MCP_PRIORITY", "high")
    session = _use_session(monkeypatch, *[_Response() for _ in range(5)])

    asyncio.run(router_bridge.chat_complete([{"role": "user", "content": "hello"}], role="utility"))
    asyncio.run(router_bridge.get_embeddings(["one", "two"]))
    asyncio.run(
        router_bridge.route_preview(
            role="coder",
            task_type="code",
            requires_tools=True,
            estimated_tokens=321,
            local_only=True,
        )
    )
    asyncio.run(router_bridge.compute_budget())
    asyncio.run(router_bridge.fleet_status())

    headers = {
        "Content-Type": "application/json",
        "X-Agent-ID": "mcp-test",
        "X-Agent-Type": "mcp",
        "X-App-Id": "mcp",
        "X-Priority": "high",
        "Authorization": "Bearer secret-key",
    }
    assert [(method, url) for method, url, _ in session.calls] == [
        ("POST", "http://router.test:9100/v1/chat/completions"),
        ("POST", "http://router.test:9100/v1/embeddings"),
        ("POST", "http://router.test:9100/routing/request"),
        ("GET", "http://router.test:9100/compute/budget"),
        ("GET", "http://router.test:9100/fleet/status"),
    ]
    assert [call[2]["headers"] for call in session.calls] == [headers] * 5
    assert session.calls[0][2]["json"] == {
        "model": "utility",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 2048,
        "temperature": 0.7,
        "stream": False,
        "routing": {"role": "utility", "agent_type": "mcp"},
    }
    assert session.calls[1][2]["json"] == {"input": ["one", "two"], "model": "embedding"}
    assert session.calls[2][2]["json"] == {
        "agent_id": "mcp-router",
        "agent_type": "mcp",
        "role": "coder",
        "task_type": "code",
        "requires_tools": True,
        "requires_vision": False,
        "requires_json_mode": False,
        "estimated_tokens": 321,
        "routing_strategy": "balanced_local",
        "local_only": True,
    }
    assert session.calls[3][2]["json"] is None
    assert session.calls[4][2]["json"] is None
    assert [call[2]["timeout"].total for call in session.calls] == [120, 60, 30, 30, 30]


@pytest.mark.parametrize(
    ("status", "code", "detail"),
    [
        (401, "router_unauthorized", "Router authentication failed"),
        (429, "router_rate_limited", "Router admission limit reached"),
        (503, "router_unavailable", "Router service unavailable"),
    ],
)
def test_bridge_maps_router_http_errors_without_returning_body(monkeypatch, status, code, detail):
    _use_session(
        monkeypatch,
        _Response(status, {"error": "raw", "detail": "SECRET PROMPT AND INTERNAL PATH"}),
    )

    result = asyncio.run(router_bridge.fleet_status())

    assert result == {"error": code, "detail": detail, "status": status}
    assert "secret" not in str(result).lower()
    assert "prompt" not in str(result).lower()


def test_bridge_maps_timeout_and_malformed_json_to_sanitized_gateway_errors(monkeypatch):
    _use_session(monkeypatch, TimeoutError("SECRET TIMEOUT PATH"))
    timeout = asyncio.run(router_bridge.compute_budget())
    _use_session(monkeypatch, _Response(json_error=ValueError("SECRET INVALID BODY")))
    malformed = asyncio.run(router_bridge.compute_budget())

    assert timeout == {
        "error": "router_timeout",
        "detail": "Router request timed out",
        "status": 504,
    }
    assert malformed == {
        "error": "router_invalid_response",
        "detail": "Router returned invalid JSON",
        "status": 502,
    }
    assert "secret" not in str((timeout, malformed)).lower()


def test_route_task_never_sends_task_text_as_router_metadata(monkeypatch):
    calls = []

    async def fake_request(method, path, payload=None, timeout=30):
        calls.append((method, path, payload, timeout))
        return {"selected_model": "local"}

    monkeypatch.setattr(router_bridge, "_router_request", fake_request)
    result = asyncio.run(router_bridge.route_task(task="SECRET RAW TASK", est_input_tokens=10))

    assert result == {"selected_model": "local"}
    assert calls[0][0:2] == ("POST", "/routing/request")
    assert "metadata" not in calls[0][2]
    assert "SECRET RAW TASK" not in str(calls)


def test_mcp_tools_hide_mutations_by_default():
    from local_model_router.mcp.tools import register_tools

    read_only = _Registrar()
    register_tools(read_only)
    mutating = _Registrar()
    register_tools(mutating, allow_mutating_tools=True)

    mutation_names = {"start_fleet", "start_slot", "stop_slot"}
    assert mutation_names.isdisjoint(read_only.tools)
    assert mutation_names <= mutating.tools.keys()
    assert {"chat_completion", "get_embeddings", "route_preview", "compute_budget", "fleet_status"} <= read_only.tools.keys()


def test_mcp_tool_wrappers_delegate_without_local_fleet_manager(monkeypatch):
    from local_model_router.mcp import tools

    calls = []

    def fake(name, result):
        async def invoke(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result

        return invoke

    monkeypatch.setattr(tools.bridge, "chat_complete", fake("chat", {"chat": True}))
    monkeypatch.setattr(tools.bridge, "get_embeddings", fake("embeddings", {"embeddings": True}))
    monkeypatch.setattr(tools.bridge, "fleet_status", fake("fleet", {"fleet": True}))
    monkeypatch.setattr(tools.bridge, "start_fleet", fake("start_fleet", {"ok": True}))
    monkeypatch.setattr(tools.bridge, "start_slot", fake("start_slot", {"ok": True}))
    monkeypatch.setattr(tools.bridge, "stop_slot", fake("stop_slot", {"ok": True}))
    monkeypatch.setattr(
        tools.bridge,
        "slot_configs",
        fake("slots", {"chat": {"role": "chat", "port": 8001, "model_id": "m", "enabled": True}}),
    )
    monkeypatch.setattr(
        tools.bridge,
        "list_models",
        fake(
            "models",
            {
                "models": [
                    {
                        "id": "match",
                        "source": "local_fleet",
                        "context_size": 8192,
                        "capabilities": {"tools": True},
                    },
                    {
                        "id": "skip",
                        "source": "upstream",
                        "context_size": 1024,
                        "capabilities": {},
                    },
                ]
            },
        ),
    )
    monkeypatch.setattr(tools.bridge, "model_card", fake("model_card", {"id": "m"}))
    monkeypatch.setattr(tools.bridge, "providers_list", fake("providers", {"providers": []}))
    monkeypatch.setattr(tools.bridge, "route_preview", fake("preview", {"selected": "m"}))
    monkeypatch.setattr(tools.bridge, "compute_budget", fake("budget", {"status": "ok"}))
    monkeypatch.setattr(tools.bridge, "route_task", fake("task", {"selected": "m"}))

    registrar = _Registrar()
    tools.register_tools(registrar, allow_mutating_tools=True)
    run = lambda name, *args, **kwargs: asyncio.run(registrar.tools[name](*args, **kwargs))

    assert run("chat_completion", [{"role": "user", "content": "hello"}], system_prompt="system") == {"chat": True}
    assert run("utility_completion", [{"role": "user", "content": "hello"}]) == {"chat": True}
    assert run("route_completion", [{"content": "one"}], role="embedding") == {"embeddings": True}
    assert run("route_completion", [{"content": "one"}], role="chat") == {"chat": True}
    assert run("get_embeddings", ["one"]) == {"embeddings": True}
    assert run("fleet_status") == {"fleet": True}
    assert run("start_fleet") == {"ok": True}
    assert run("start_slot", "chat") == {"ok": True}
    assert run("stop_slot", "chat") == {"ok": True}
    assert run("list_slots") == {
        "chat": {"role": "chat", "port": 8001, "model_id": "m", "enabled": True}
    }
    assert run("list_models", capability="tools", source="local_fleet", min_context=4096) == {
        "models": [
            {
                "id": "match",
                "source": "local_fleet",
                "context_size": 8192,
                "capabilities": {"tools": True},
            }
        ],
        "count": 1,
    }
    assert run("model_card", "m") == {"id": "m"}
    assert run("providers_list") == {"providers": []}
    assert run("route_preview", role="coder") == {"selected": "m"}
    assert run("compute_budget") == {"status": "ok"}
    assert run("route_task", task="not telemetry") == {"selected": "m"}
    assert calls[0][2]["messages"][0] == {"role": "system", "content": "system"}


def test_mcp_resources_delegate_to_http_bridge(monkeypatch):
    from local_model_router.mcp import resources

    async def fleet_status():
        return {"slots": [{"id": "chat"}]}

    async def slot_configs():
        return {
            "chat": {
                "role": "chat",
                "port": 8001,
                "model_id": "m",
                "context_size": 4096,
                "enabled": True,
            }
        }

    async def hardware_profile():
        return {"gpus": []}

    monkeypatch.setattr(resources.bridge, "fleet_status", fleet_status)
    monkeypatch.setattr(resources.bridge, "slot_configs", slot_configs)
    monkeypatch.setattr(resources.bridge, "hardware_profile", hardware_profile)
    registrar = _Registrar()
    resources.register_resources(registrar)

    assert '"chat"' in asyncio.run(registrar.resources["models://fleet/status"]())
    assert '"model_id": "m"' in asyncio.run(registrar.resources["models://{slot_id}/info"]("chat"))
    assert "not found" in asyncio.run(registrar.resources["models://{slot_id}/info"]("missing"))
    assert '"gpus": []' in asyncio.run(registrar.resources["models://hardware/profile"]())
    assert '"id": "chat"' in asyncio.run(registrar.resources["models://slots/list"]())


def test_bridge_has_no_backend_manager_path():
    source = inspect.getsource(router_bridge)
    assert "BackendManager" not in source
    assert "llama_cpp_manager" not in source
    assert not hasattr(router_bridge, "_get_manager")


def test_core_app_builds_when_mcp_extra_is_unavailable(tmp_path):
    config = tmp_path / "fleet.yaml"
    config.write_text("global:\n  backend: remote\nactive_slots: []\n", encoding="utf-8")
    env = os.environ.copy()
    env["A0_FLEET_STATE_DB"] = str(tmp_path / "fleet.sqlite3")
    env["A0_AGENT_ORCHESTRATOR_WORKSPACE"] = str(tmp_path / "orchestrator")
    code = """
import builtins
import sys

real_import = builtins.__import__
def without_mcp(name, *args, **kwargs):
    if name == 'mcp' or name.startswith('mcp.'):
        raise ModuleNotFoundError("optional MCP extra unavailable")
    return real_import(name, *args, **kwargs)
builtins.__import__ = without_mcp

from local_model_router.service.app import create_app
assert create_app(sys.argv[1]) is not None
"""

    result = subprocess.run(
        [sys.executable, "-c", code, str(config)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
