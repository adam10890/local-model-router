"""Contract tests for /openapi.json, OpenAI surfaces, and harness pin-drift alerts."""
from __future__ import annotations

import aiohttp
from starlette.testclient import TestClient

from local_model_router import __version__
from local_model_router.service.app import create_app
from local_model_router.service.readiness import build_ui_status


def _setup_files(tmp_path, *, harnesses_yaml: str | None = None):
    conf = tmp_path / "llama_cpp_servers.yaml"
    conf.write_text(
        """active_slots:
  - id: slot_chat
    host: 127.0.0.1
    port: 18080
    role: chat
    enabled: true
    model_id: qwen-local
    context_size: 8192
global:
  backend: remote
""",
        encoding="utf-8",
    )
    (tmp_path / "apps.yaml").write_text("apps: {}\n", encoding="utf-8")
    (tmp_path / "upstreams.yaml").write_text("upstreams: {}\n", encoding="utf-8")
    harnesses = tmp_path / "harnesses.yaml"
    harnesses.write_text(
        harnesses_yaml
        or """harnesses:
  hermes:
    display_name: Hermes
    kind: hermes
    protocol: openai
    location: host
    connections:
      default:
        model: qwen-local
""",
        encoding="utf-8",
    )
    setup_home = tmp_path / "setup"
    (setup_home / "bin").mkdir(parents=True, exist_ok=True)
    return conf, harnesses, setup_home


def test_openapi_spec_requires_auth_when_key_set_and_describes_live_routes(tmp_path, monkeypatch):
    conf, harnesses, setup_home = _setup_files(tmp_path)
    monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", "secret-key")
    monkeypatch.setattr("local_model_router.service.app.inspect_running_llama_models", lambda: {})
    app = create_app(
        str(conf),
        harnesses_path=str(harnesses),
        setup_home=str(setup_home),
        setup_api_enabled=False,
    )
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 401
        resp = client.get("/openapi.json", headers={"Authorization": "Bearer secret-key"})
        assert resp.status_code == 200
        spec = resp.json()

    assert spec["openapi"] == "3.1.0"
    assert spec["info"]["version"] == __version__
    assert spec["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
    }
    paths = spec["paths"]
    for expected in (
        "/health",
        "/v1/models",
        "/v1/chat/completions",
        "/v1/embeddings",
        "/harnesses/{harness_id}/v1/chat/completions",
        "/harnesses",
        "/ui/status",
        "/diagnostics/report",
        "/openapi.json",
    ):
        assert expected in paths
    assert paths["/health"]["get"]["security"] == []
    assert "401" not in paths["/health"]["get"]["responses"]
    assert paths["/v1/chat/completions"]["post"]["security"] == [{"BearerAuth": []}]
    op_ids = [
        op["operationId"]
        for path_item in paths.values()
        for op in path_item.values()
    ]
    assert op_ids
    assert len(op_ids) == len(set(op_ids))
    serialized = resp.text.lower()
    assert "secret-key" not in serialized
    assert str(tmp_path).lower() not in serialized


def test_openai_and_harness_contract_shapes(tmp_path, monkeypatch):
    conf, harnesses, setup_home = _setup_files(tmp_path)

    async def healthy(_url, _timeout):
        return {"ok": True}

    monkeypatch.setattr("local_model_router.helpers.smart_router.health._aiohttp_probe", healthy)

    class FakeResponse:
        status = 200

        def __init__(self, body):
            self._body = body

        async def json(self, content_type=None):
            return self._body

        async def text(self):
            return str(self._body)

    class FakeCtx:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *exc):
            return None

    class FakeSession:
        def post(self, url, json=None, **_kwargs):
            if url.endswith("/embeddings"):
                return FakeCtx(
                    FakeResponse(
                        {
                            "object": "list",
                            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                            "model": (json or {}).get("model", "qwen-local"),
                            "usage": {"prompt_tokens": 3, "total_tokens": 3},
                        }
                    )
                )
            return FakeCtx(
                FakeResponse(
                    {
                        "id": "chatcmpl-contract",
                        "object": "chat.completion",
                        "model": (json or {}).get("model", "qwen-local"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                    }
                )
            )

        async def close(self):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_kw: FakeSession())

    async def fake_fetch(_base_url):
        return [{"id": "qwen-local"}]

    app = create_app(
        str(conf),
        harnesses_path=str(harnesses),
        setup_home=str(setup_home),
        models_fetch=fake_fetch,
        setup_api_enabled=False,
    )
    with TestClient(app) as client:
        models = client.get("/v1/models")
        assert models.status_code == 200
        models_body = models.json()
        assert models_body["object"] == "list"
        assert any(row["object"] == "model" and row["id"] == "qwen-local" for row in models_body["data"])

        chat = client.post(
            "/v1/chat/completions",
            json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert chat.status_code == 200
        chat_body = chat.json()
        assert chat_body["choices"][0]["message"]["role"] == "assistant"
        assert isinstance(chat_body["usage"]["prompt_tokens"], int)

        emb = client.post("/v1/embeddings", json={"model": "local", "input": "hello"})
        assert emb.status_code == 200
        assert emb.json()["object"] == "list"
        assert emb.json()["data"][0]["embedding"] == [0.1, 0.2]

        harness_models = client.get("/harnesses/hermes/v1/models")
        assert harness_models.status_code == 200
        assert harness_models.json()["object"] == "list"
        assert any(
            row["id"] == "local" and row["meta"]["pinned_model"] == "qwen-local"
            for row in harness_models.json()["data"]
        )

        harness_chat = client.post(
            "/harnesses/hermes/v1/chat/completions",
            json={"model": "ignored-client-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert harness_chat.status_code == 200
        assert harness_chat.json()["choices"][0]["message"]["role"] == "assistant"

        bad_emb = client.post("/v1/embeddings", json={"model": "local"})
        assert bad_emb.status_code == 400
        assert set(bad_emb.json()["error"]) >= {"message", "type", "code"}

        bad_harness = client.post("/harnesses/missing-harness/v1/chat/completions", json={"messages": []})
        assert bad_harness.status_code == 404
        assert bad_harness.json()["error"] == "unknown_harness"


def test_harness_pin_drift_surfaces_optional_issue_without_downgrading_ready_or_leaking_secrets(
    tmp_path, monkeypatch
):
    conf, harnesses, setup_home = _setup_files(
        tmp_path,
        harnesses_yaml="""harnesses:
  hermes:
    display_name: Hermes
    kind: hermes
    protocol: openai
    location: host
    connections:
      default:
        model: missing-drifted-model
""",
    )
    secret_hint = "sk-secret-value-never-leak"
    monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", secret_hint)

    class StubHardware:
        def vram_summary(self):
            return {"available_gb": 8.0, "total_gb": 12.0}

        def to_dict(self):
            return {
                "available": True,
                "gpus": [{"id": 0, "name": "Test GPU", "total_vram_mb": 12288, "free_vram_mb": 8192}],
                "cpu": {"utilization_pct": 10},
                "ram": {"total_mb": 32768, "available_mb": 20000, "used_mb": 12768, "utilization_pct": 39},
            }

    async def healthy(_url, _timeout):
        return {"ok": True}

    monkeypatch.setattr("local_model_router.helpers.smart_router.health._aiohttp_probe", healthy)
    monkeypatch.setattr("local_model_router.service.app.scan_hardware", lambda: StubHardware())
    monkeypatch.setattr("local_model_router.service.app.inspect_running_llama_models", lambda: {18080: "qwen-local"})

    app = create_app(
        str(conf),
        harnesses_path=str(harnesses),
        setup_home=str(setup_home),
        setup_api_enabled=False,
    )
    app.state.setup_engine.state = lambda: {
        "discovery": {
            "runtime_installed": True,
            "gguf_models": [{"id": "qwen-local"}],
            "config_exists": True,
            "enabled_slots": 1,
            "servers": [],
        },
        "hardware": {
            "gpus": [{"name": "Test GPU", "memory_mb": 12288}],
            "ram": {"total_mb": 32768, "available_mb": 20000},
        },
    }

    with TestClient(app) as client:
        status_resp = client.get("/ui/status", headers={"Authorization": f"Bearer {secret_hint}"})
        assert status_resp.status_code == 200
        status_body = status_resp.json()
        assert status_body["overall"] == "ready"
        pin_issues = [i for i in status_body["optional_issues"] if i["code"] == "harness_pin_drift"]
        assert len(pin_issues) == 1
        assert pin_issues[0]["category"] == "configuration"
        assert pin_issues[0]["severity"] == "optional"
        assert pin_issues[0]["action"]["href"] == "#/connections"
        assert pin_issues[0]["affected_harnesses"] == ["hermes"]

        diag = client.get("/diagnostics/report", headers={"Authorization": f"Bearer {secret_hint}"})
        assert diag.status_code == 200
        assert any(i["code"] == "harness_pin_drift" for i in diag.json()["readiness"]["optional_issues"])
        assert secret_hint not in diag.text
        assert str(tmp_path) not in diag.text

    # Direct unit verification: resolved pins emit no harness_pin_drift issue.
    clean = build_ui_status(
        setup_state={"discovery": {"runtime_installed": True, "gguf_models": [{"id": "m"}], "config_exists": True, "enabled_slots": 1}},
        slots_health=[{"id": "slot_chat", "enabled": True, "health": "healthy", "port": 18080, "model_id": "m"}],
        compute={"available": True, "gpus": []},
        base_url="http://127.0.0.1:9000",
        harnesses=[{"harness_id": "hermes", "connections": [{"name": "default", "pin_valid": True}]}],
    )
    assert not [i for i in clean["optional_issues"] if i["code"] == "harness_pin_drift"]
