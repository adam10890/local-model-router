"""Tests for the standalone Fleet Manager control-plane layer."""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "local_model_router"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from starlette.testclient import TestClient  # noqa: E402


_ROUTING_CONFIG = """\
active_slots:
  - id: chat
    port: 8080
    host: localhost
    role: chat
    enabled: true
    model_id: chat-model
    context_size: 65536
  - id: utility
    port: 8088
    host: localhost
    role: utility
    enabled: true
    model_id: utility-model
    context_size: 32768
global:
  backend: remote
"""


def _hardware_snapshot():
    from local_model_router.helpers.compute_monitor import CPUStats, ComputeSnapshot, GPUStats

    return ComputeSnapshot(
        timestamp=1_750_000_000.25,
        gpus=(GPUStats(0, "Test GPU", 24576, 6144, 18432, 37.0, 49.0),),
        cpu=CPUStats(25.0, 32768, 12288, 20480),
    )


def _make_client(tmp_path, monkeypatch, queue=None, hardware_scan=None):
    from local_model_router.service.app import create_app
    from local_model_router.service.fleet_manager import FleetQueue, FleetStore
    from local_model_router.helpers.llama_cpp_manager import BackendManager

    BackendManager._instance = None
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(_ROUTING_CONFIG, encoding="utf-8")

    async def health_probe(url, timeout):
        return {"ok": True}

    monkeypatch.setattr(
        "local_model_router.helpers.smart_router.health._aiohttp_probe",
        health_probe,
    )
    monkeypatch.setattr(
        "local_model_router.service.app.scan_hardware",
        hardware_scan or _hardware_snapshot,
    )
    store = FleetStore(str(tmp_path / "fleet.sqlite3"))
    app = create_app(str(cfg), fleet_store=store, fleet_queue=queue or FleetQueue(max_active=1, max_queue=4))
    return TestClient(app), store, BackendManager


def _patch_forward(monkeypatch, status=200, payload=None):
    import aiohttp

    calls = []
    response_payload = payload or {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    }

    class FakeResponse:
        def __init__(self):
            self.status = status

        async def json(self, content_type=None):
            return response_payload

        async def text(self):
            return "not-json"

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

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: FakeSession())
    return calls


def test_identity_headers_override_body_metadata():
    from local_model_router.service.fleet_manager import identity_from_headers

    agent = identity_from_headers(
        {
            "x-agent-id": "hermes-1",
            "x-agent-type": "hermes",
            "x-priority": "high",
        },
        {
            "metadata": {
                "agent_id": "body-agent",
                "agent_type": "custom",
                "priority": "low",
            }
        },
    )

    assert agent.agent_id == "hermes-1"
    assert agent.agent_type == "hermes"
    assert agent.priority == "high"


def test_invalid_priority_is_rejected():
    from local_model_router.service.fleet_manager import identity_from_headers

    try:
        identity_from_headers({"x-priority": "urgent"})
        assert False, "invalid priority should raise"
    except ValueError as exc:
        assert "invalid priority" in str(exc)


def test_fleet_agents_register_and_list(tmp_path, monkeypatch):
    client, _store, manager_cls = _make_client(tmp_path, monkeypatch)

    resp = client.post(
        "/fleet/agents/register",
        json={
            "agent_id": "a0-main",
            "agent_type": "agent-zero",
            "priority": "normal",
            "metadata": {"container": "agent-zero-1"},
        },
    )

    assert resp.status_code == 200
    assert resp.json()["agent"]["agent_id"] == "a0-main"
    agents = client.get("/fleet/agents").json()["agents"]
    assert agents[0]["metadata"]["container"] == "agent-zero-1"
    manager_cls._instance = None


def test_fleet_status_reports_queue_agents_and_slots(tmp_path, monkeypatch):
    client, _store, manager_cls = _make_client(tmp_path, monkeypatch)
    client.post("/fleet/agents/register", json={"agent_id": "hermes-1", "agent_type": "hermes"})

    resp = client.get("/fleet/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "a0-fleet-manager"
    assert body["docker_socket_enabled"] is False
    assert body["queue"]["max_active"] == 1
    assert body["queues"]["local"] == {"mode": "bounded", **body["queue"]}
    assert body["agents"]["count"] == 1
    assert body["vram"] == {
        "total_gb": 24.0,
        "used_gb": 6.0,
        "available_gb": 18.0,
        "source": "nvidia-smi",
    }
    assert body["compute"]["available"] is True
    assert body["compute"]["cpu"]["utilization_pct"] == 25.0
    assert body["compute"]["ram"]["available_mb"] == 20480
    assert body["model_residency"][0]["slot_id"] == "chat"
    assert body["context_windows"][0]["hard_ctx"] == 65536
    assert body["context_windows"][0]["effective_ctx"] == 45875
    manager_cls._instance = None


def test_health_still_reports_service_identity(tmp_path, monkeypatch):
    client, _store, manager_cls = _make_client(tmp_path, monkeypatch)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["service"] == "lmm-router-observer"
    manager_cls._instance = None


def test_fleet_status_reuses_recent_hardware_snapshot(tmp_path, monkeypatch):
    calls = 0

    def scan():
        nonlocal calls
        calls += 1
        return _hardware_snapshot()

    client, _store, manager_cls = _make_client(
        tmp_path,
        monkeypatch,
        hardware_scan=scan,
    )

    assert client.get("/fleet/status").status_code == 200
    assert client.get("/fleet/status").status_code == 200
    assert calls == 1
    manager_cls._instance = None


def test_fleet_status_survives_hardware_probe_failure(tmp_path, monkeypatch):
    def failing_scan():
        raise RuntimeError("probe failed")

    client, _store, manager_cls = _make_client(
        tmp_path,
        monkeypatch,
        hardware_scan=failing_scan,
    )

    response = client.get("/fleet/status")

    assert response.status_code == 200
    assert response.json()["vram"] == {
        "total_gb": None,
        "used_gb": None,
        "available_gb": None,
        "source": "not_configured",
    }
    assert response.json()["compute"] == {
        "available": False,
        "timestamp": None,
        "gpus": [],
        "cpu": None,
        "ram": None,
    }
    manager_cls._instance = None


def test_chat_completions_records_agent_and_adds_fleet_headers(tmp_path, monkeypatch):
    client, store, manager_cls = _make_client(tmp_path, monkeypatch)
    calls = _patch_forward(monkeypatch)

    resp = client.post(
        "/v1/chat/completions",
        headers={
            "X-Agent-ID": "hermes-1",
            "X-Agent-Type": "hermes",
            "X-Priority": "high",
        },
        json={"model": "local-chat", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert resp.status_code == 200
    assert resp.headers["x-a0-agent-id"] == "hermes-1"
    assert resp.headers["x-a0-agent-type"] == "hermes"
    assert resp.headers["x-a0-fleet-queue-depth"] == "0"
    assert resp.headers["x-a0-router-slot-id"] == "chat"
    assert resp.headers["x-selected-model"] == "chat-model"
    assert resp.headers["x-hard-ctx"] == "65536"
    assert resp.headers["x-effective-ctx"] == "45875"
    assert calls[0]["args"][0] == "http://localhost:8080/v1/chat/completions"
    # explicit non-alias model id passes through verbatim
    assert calls[0]["kwargs"]["json"]["model"] == "local-chat"
    assert store.get_agent("hermes-1")["request_count"] == 1
    assert store.request_summary()["by_status"]["completed"] == 1
    manager_cls._instance = None


def test_chat_completions_queue_full_returns_429(tmp_path, monkeypatch):
    from local_model_router.service.fleet_manager import FleetQueue

    client, store, manager_cls = _make_client(tmp_path, monkeypatch, queue=FleetQueue(max_active=0, max_queue=0))

    resp = client.post(
        "/v1/chat/completions",
        headers={"X-Agent-ID": "a0-main"},
        json={"messages": [{"role": "user", "content": "hello"}]},
    )

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "queue_full"
    assert store.request_summary()["by_status"]["rejected"] == 1
    manager_cls._instance = None


def test_fleet_store_creates_sqlite_tables(tmp_path):
    from local_model_router.service.fleet_manager import AgentIdentity, FleetStore

    db = tmp_path / "fleet.sqlite3"
    store = FleetStore(str(db))
    request_id = store.create_request(AgentIdentity(agent_id="agent-1", agent_type="custom", priority="low"))
    store.update_request(request_id, status="completed", duration_ms=12)

    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }

    assert {"agents", "requests", "queue_events", "model_residency_snapshots"}.issubset(tables)
    assert store.request_summary()["by_status"]["completed"] == 1


def test_fleet_store_migrates_lane_fields_and_returns_latest_snapshot(tmp_path):
    from local_model_router.service.fleet_manager import AgentIdentity, FleetStore

    store = FleetStore(str(tmp_path / "fleet.sqlite3"))
    request_id = store.create_request(AgentIdentity(agent_id="agent-1"))
    store.update_request(
        request_id,
        status="completed",
        upstream_name="dmr",
        admission_lane="upstream:dmr",
    )
    store.record_model_snapshot("model_evaluation", {"schema_version": 1})

    analytics = store.routing_analytics()
    assert analytics["recent"][0]["admission_lane"] == "upstream:dmr"
    assert analytics["by_upstream"] == {"dmr": 1}
    assert store.latest_model_snapshot("model_evaluation")["payload"]["schema_version"] == 1


def test_fleet_queue_prioritizes_waiters():
    from local_model_router.service.fleet_manager import FleetQueue

    async def scenario():
        queue = FleetQueue(max_active=1, max_queue=2)
        first = await queue.acquire("normal")
        assert first.active_at_admit == 1

        order = []

        async def wait(priority, label):
            await queue.acquire(priority)
            order.append(label)
            await queue.release()

        low = asyncio.create_task(wait("low", "low"))
        await asyncio.sleep(0)
        high = asyncio.create_task(wait("high", "high"))
        await asyncio.sleep(0)
        await queue.release()
        await asyncio.gather(low, high)
        return order

    assert asyncio.run(scenario()) == ["high", "low"]
