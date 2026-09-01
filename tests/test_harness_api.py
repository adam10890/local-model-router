"""Dedicated harness setup and OpenAI-compatible routes."""
from __future__ import annotations

import textwrap

import pytest
from starlette.testclient import TestClient

from local_model_router.service.app import create_app

_HARNESSES = """\
harnesses:
  hermes:
    display_name: Hermes
    kind: hermes
    protocol: openai
    location: host
    connections:
      default: {model: ornith}
  pi:
    display_name: Pi
    kind: pi
    protocol: openai
    location: host
    connections:
      default: {model: utility_cpu}
  agent_zero:
    display_name: Agent Zero
    kind: agent_zero
    protocol: openai
    location: docker
    connections:
      chat: {model: ornith}
      utility: {model: utility_cpu}
  hermes_dmr:
    display_name: Hermes DMR
    kind: hermes
    protocol: openai
    location: host
    connections:
      default: {model: dmr/ornith}
"""

_FLEET = """\
active_slots:
  - id: chat
    host: localhost
    port: 8080
    role: chat
    enabled: true
    model_id: chat-model
  - id: ornith
    host: localhost
    port: 8081
    role: chat
    enabled: true
    model_id: ornith
    context_size: 131072
    mmproj_path: ornith-mmproj.gguf
    supports_vision: true
    supports_tools: true
  - id: utility
    host: localhost
    port: 8088
    role: utility
    enabled: true
    model_id: utility_cpu
global:
  backend: remote
"""

_UPSTREAMS = """\
upstreams:
  - name: dmr
    type: openai_compatible
    base_url: http://localhost:12434/engines/v1
    enabled: true
"""


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return str(path)


def _client(tmp_path, monkeypatch, *, api_key=None, utility_healthy=True):
    from local_model_router.helpers.llama_cpp_manager import BackendManager

    BackendManager._instance = None
    if api_key is None:
        monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", api_key)

    async def health_probe(url, timeout):
        return {"ok": utility_healthy or "8088" not in url}

    monkeypatch.setattr("local_model_router.helpers.smart_router.health._aiohttp_probe", health_probe)
    app = create_app(
        _write(tmp_path / "llama_cpp_servers.yaml", _FLEET),
        harnesses_path=_write(tmp_path / "harnesses.yaml", _HARNESSES),
        upstreams_path=_write(tmp_path / "upstreams.yaml", _UPSTREAMS),
        apps_path=str(tmp_path / "missing-apps.yaml"),
    )
    return TestClient(app)


def _patch_forward(monkeypatch):
    import aiohttp

    calls = []

    class FakeResponse:
        status = 200

        async def json(self, content_type=None):
            return {"id": "chatcmpl-harness", "choices": [{"message": {"content": "ok"}}]}

        async def text(self):
            return "ok"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FakeSession:
        def post(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *args, **kwargs: FakeSession())
    return calls


def test_harness_list_and_detail_emit_exact_setup_urls(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.get("/harnesses")
    assert response.status_code == 200
    by_id = {item["harness_id"]: item for item in response.json()["harnesses"]}
    assert by_id["hermes"]["connections"][0]["base_url"] == (
        "http://127.0.0.1:9000/harnesses/hermes/v1"
    )
    agent_zero = by_id["agent_zero"]
    assert agent_zero["connections"][0]["base_url"].startswith(
        "http://host.docker.internal:9000/harnesses/agent_zero/"
    )
    setup = agent_zero["setup"]["content"]
    assert "provider=other" in setup
    assert "/agent_zero/chat/v1" in setup
    assert "/agent_zero/utility/v1" in setup

    detail = client.get("/harnesses/pi").json()
    assert detail["connections"][0]["model"] == "utility_cpu"
    assert detail["connections"][0]["client_model"] == "local"
    assert "models.json" in detail["setup"]["target"]
    assert "secret" not in str(detail).lower()


def test_single_connection_models_endpoint_lists_only_stable_client_model(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.get("/harnesses/hermes/v1/models")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == ["local"]
    row = response.json()["data"][0]
    assert row["meta"]["pinned_model"] == "ornith"
    assert row["capabilities"]["vision"] is True
    assert row["capabilities"]["tools"] is True
    detail = client.get("/harnesses/hermes").json()
    assert detail["connections"][0]["verification"]["state"] == "connected"
    assert "supports_vision: true" in detail["setup"]["content"]
    assert "context_length: 131072" in detail["setup"]["content"]
    assert detail["readiness"]["vision_ready"] is True
    assert detail["readiness"]["lifecycle_managed"] is True


def test_upstream_mmproj_error_is_capability_missing_not_unavailable(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    import aiohttp

    class FakeResponse:
        status = 500

        async def json(self, content_type=None):
            return {
                "error": {
                    "message": "image input is not supported ... you may need to provide the mmproj",
                }
            }

        async def text(self):
            return "mmproj"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FakeSession:
        def post(self, *args, **kwargs):
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *args, **kwargs: FakeSession())
    response = client.post(
        "/harnesses/hermes_dmr/v1/chat/completions",
        json={"model": "local", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "upstream_capability_missing"
    assert "mmproj" in body["upstream_message"].lower() or "image input" in body["error"]["message"].lower()


def test_agent_zero_requires_named_connection_and_supports_chat(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/harnesses/agent_zero/v1/models").status_code == 404
    response = client.get("/harnesses/agent_zero/chat/v1/models")
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "local"


def test_unknown_harness_and_connection_are_explainable_404s(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    missing = client.get("/harnesses/missing")
    assert missing.status_code == 404
    assert missing.json()["error"] == "unknown_harness"
    connection = client.get("/harnesses/agent_zero/planner/v1/models")
    assert connection.status_code == 404
    assert connection.json()["error"] == "unknown_harness_connection"


def test_dedicated_upstream_chat_ignores_client_model_and_routing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls = _patch_forward(monkeypatch)
    response = client.post(
        "/harnesses/hermes_dmr/v1/chat/completions",
        json={
            "model": "try-to-escape",
            "messages": [{"role": "user", "content": "hi"}],
            "routing": {"preferred_slot": "utility", "role": "utility"},
        },
    )
    assert response.status_code == 200
    assert calls[0]["args"][0].endswith("/chat/completions")
    assert calls[0]["kwargs"]["json"]["model"] == "ornith"
    assert "routing" not in calls[0]["kwargs"]["json"]


def test_dedicated_local_chat_pins_matching_slot(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls = _patch_forward(monkeypatch)
    response = client.post(
        "/harnesses/hermes/v1/chat/completions",
        json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    assert calls[0]["args"][0] == "http://localhost:8081/v1/chat/completions"
    assert calls[0]["kwargs"]["json"]["model"] == "ornith"
    assert response.headers["x-a0-router-slot-id"] == "ornith"


def test_dedicated_utility_slot_still_pins(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls = _patch_forward(monkeypatch)
    response = client.post(
        "/harnesses/pi/v1/chat/completions",
        json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    assert calls[0]["args"][0] == "http://localhost:8088/v1/chat/completions"
    assert calls[0]["kwargs"]["json"]["model"] == "utility_cpu"
    assert response.headers["x-a0-router-slot-id"] == "utility"


def test_unavailable_pinned_slot_does_not_fall_back(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, utility_healthy=False)
    calls = _patch_forward(monkeypatch)
    response = client.post(
        "/harnesses/pi/v1/chat/completions",
        json={"model": "anything", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "harness_model_unavailable"
    assert response.json()["pinned_model"] == "utility_cpu"
    assert calls == []


def test_harness_routes_honor_router_auth(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, api_key="secret")
    assert client.get("/harnesses").status_code == 401
    assert client.get(
        "/harnesses", headers={"Authorization": "Bearer secret"}
    ).status_code == 200


def test_harness_create_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_ENABLE_CONFIG_WRITES", raising=False)
    client = _client(tmp_path, monkeypatch, api_key="secret")
    response = client.post(
        "/harnesses",
        headers={"Authorization": "Bearer secret"},
        json={
            "harness_id": "new_harness",
            "connections": {"default": {"model": "utility_cpu"}},
        },
    )
    assert response.status_code == 403
    assert response.json()["error"] == "config_writes_disabled"


def test_harness_pin_is_hidden_and_disabled_without_full_write_gate(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_ENABLE_CONFIG_WRITES", raising=False)
    disabled = _client(tmp_path / "disabled", monkeypatch, api_key="secret")
    headers = {"Authorization": "Bearer secret"}
    listing = disabled.get("/harnesses", headers=headers)
    assert listing.json()["config_writes_enabled"] is False
    denied = disabled.patch(
        "/harnesses/hermes/connections/default",
        headers=headers,
        json={"model": "utility_cpu"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"] == "config_writes_disabled"

    monkeypatch.setenv("A0_LMM_ROUTER_ENABLE_CONFIG_WRITES", "1")
    no_key = _client(tmp_path / "no-key", monkeypatch)
    assert no_key.get("/harnesses").json()["config_writes_enabled"] is False
    denied = no_key.patch(
        "/harnesses/hermes/connections/default",
        json={"model": "utility_cpu"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"] == "config_write_requires_api_key"


@pytest.mark.parametrize("model", ["utility_cpu", "dmr/ornith", "coder"])
def test_harness_pin_accepts_configured_slot_upstream_and_live_alias(
    tmp_path, monkeypatch, model
):
    monkeypatch.setenv("A0_LMM_ROUTER_ENABLE_CONFIG_WRITES", "1")
    client = _client(tmp_path, monkeypatch, api_key="secret")
    path = "/harnesses/hermes/connections/default"

    assert client.patch(path, json={"model": model}).status_code == 401
    response = client.patch(
        path,
        headers={"Authorization": "Bearer secret"},
        json={"model": model},
    )

    assert response.status_code == 200
    assert response.json()["connections"][0]["model"] == model
    listing = client.get(
        "/harnesses", headers={"Authorization": "Bearer secret"}
    ).json()
    assert listing["config_writes_enabled"] is True
    assert next(
        item for item in listing["harnesses"] if item["harness_id"] == "hermes"
    )["connections"][0]["model"] == model
    assert list(tmp_path.glob("harnesses.yaml.*.bak"))


@pytest.mark.parametrize("model", ["not-configured-anywhere", "embedding"])
def test_harness_pin_rejects_unknown_target_without_writing(
    tmp_path, monkeypatch, model
):
    monkeypatch.setenv("A0_LMM_ROUTER_ENABLE_CONFIG_WRITES", "1")
    client = _client(tmp_path, monkeypatch, api_key="secret")
    response = client.patch(
        "/harnesses/hermes/connections/default",
        headers={"Authorization": "Bearer secret"},
        json={"model": model},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "unknown_pin_target"
    detail = client.get(
        "/harnesses/hermes", headers={"Authorization": "Bearer secret"}
    ).json()
    assert detail["connections"][0]["model"] == "ornith"
    assert not list(tmp_path.glob("harnesses.yaml.*.bak"))


def test_harness_create_requires_configured_auth_and_writes_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("A0_LMM_ROUTER_ENABLE_CONFIG_WRITES", "1")
    no_auth = _client(tmp_path / "no-auth", monkeypatch)
    denied = no_auth.post(
        "/harnesses",
        json={
            "harness_id": "new_harness",
            "connections": {"default": {"model": "utility_cpu"}},
        },
    )
    assert denied.status_code == 403
    assert denied.json()["error"] == "config_write_requires_api_key"

    client = _client(tmp_path / "with-auth", monkeypatch, api_key="secret")
    payload = {
        "harness_id": "new_harness",
        "display_name": "New Harness",
        "kind": "custom",
        "location": "host",
        "protocol": "openai",
        "connections": {"default": {"model": "utility_cpu"}},
    }
    assert client.post("/harnesses", json=payload).status_code == 401
    created = client.post(
        "/harnesses",
        headers={"Authorization": "Bearer secret"},
        json=payload,
    )
    assert created.status_code == 201
    assert created.json()["harness_id"] == "new_harness"
    assert client.get(
        "/harnesses/new_harness", headers={"Authorization": "Bearer secret"}
    ).status_code == 200
    assert list((tmp_path / "with-auth").glob("harnesses.yaml.*.bak"))
