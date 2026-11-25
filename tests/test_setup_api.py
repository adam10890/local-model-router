from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from starlette.testclient import TestClient

from local_model_router.service import routes_setup
from local_model_router.service.app import create_app
from local_model_router.setup import SetupEngine


def test_setup_api_is_token_protected_and_loopback_scoped(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    config = tmp_path / "missing.yaml"
    app = create_app(
        str(config),
        setup_home=str(tmp_path / "home"),
        upstreams_path=str(tmp_path / "upstreams.yaml"),
        apps_path=str(tmp_path / "apps.yaml"),
        harnesses_path=str(tmp_path / "harnesses.yaml"),
    )
    client = TestClient(app)
    assert client.get("/setup/state").status_code == 401
    response = client.get("/setup/state", headers={"X-Setup-Token": app.state.setup_token})
    assert response.status_code == 200
    assert response.json()["schema_version"] == 2


def test_dashboard_receives_ephemeral_setup_token(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    app = create_app(
        str(tmp_path / "missing.yaml"),
        setup_home=str(tmp_path / "home"),
        upstreams_path=str(tmp_path / "upstreams.yaml"),
        apps_path=str(tmp_path / "apps.yaml"),
        harnesses_path=str(tmp_path / "harnesses.yaml"),
    )
    client = TestClient(app)
    response = client.get("/ui")
    assert response.status_code == 200
    assert app.state.setup_token in response.text
    assert "__IMPERIUM_SETUP_TOKEN__" not in response.text
    assert '<link rel="icon" href="/ui/icons/cube.svg"' in response.text

    icon = client.get("/ui/icons/house.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert client.get("/ui/icons/cube.svg").status_code == 200


def test_dashboard_can_set_and_scan_models_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    app = create_app(
        str(tmp_path / "missing.yaml"),
        setup_home=str(tmp_path / "home"),
        upstreams_path=str(tmp_path / "upstreams.yaml"),
        apps_path=str(tmp_path / "apps.yaml"),
        harnesses_path=str(tmp_path / "harnesses.yaml"),
    )
    models = tmp_path / "models"
    models.mkdir()
    (models / "local.gguf").touch()

    response = TestClient(app).post("/models/directory", json={"path": str(models)})

    assert response.status_code == 200
    assert response.json()["discovery"]["local_models"][0]["id"] == "local"


def test_health_serializes_a_path_config_during_first_run(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    config = tmp_path / "missing.yaml"
    client = TestClient(
        create_app(
            config,
            setup_home=str(tmp_path / "home"),
            upstreams_path=str(tmp_path / "upstreams.yaml"),
            apps_path=str(tmp_path / "apps.yaml"),
            harnesses_path=str(tmp_path / "harnesses.yaml"),
        )
    )

    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["config_path"] == str(config)


def test_setup_storage_failure_returns_actionable_response(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)

    def deny_storage(_engine, *, refresh_hardware=False):
        raise PermissionError(13, "Access is denied", str(tmp_path / "locked"))

    monkeypatch.setattr(SetupEngine, "state", deny_storage)
    app = create_app(
        str(tmp_path / "missing.yaml"),
        setup_home=str(tmp_path / "home"),
        upstreams_path=str(tmp_path / "upstreams.yaml"),
        apps_path=str(tmp_path / "apps.yaml"),
        harnesses_path=str(tmp_path / "harnesses.yaml"),
    )
    client = TestClient(app)

    response = client.post(
        "/setup/scan",
        headers={"X-Setup-Token": app.state.setup_token},
    )

    assert response.status_code == 503
    assert response.json()["error"] == "setup_storage_unavailable"
    assert "writable Imperium data folder" in response.json()["remediation"]


def test_successful_smoke_disables_the_temporary_setup_api(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    monkeypatch.setattr(SetupEngine, "smoke", lambda _engine: {"ok": True})
    app = create_app(
        str(tmp_path / "missing.yaml"),
        setup_home=str(tmp_path / "home"),
        upstreams_path=str(tmp_path / "upstreams.yaml"),
        apps_path=str(tmp_path / "apps.yaml"),
        harnesses_path=str(tmp_path / "harnesses.yaml"),
    )
    client = TestClient(app)
    headers = {"X-Setup-Token": app.state.setup_token}

    assert client.post("/setup/smoke", headers=headers, json={}).json() == {"ok": True}
    response = client.get("/setup/state", headers=headers)

    assert response.status_code == 410
    assert response.json() == {"error": "setup_api_inactive"}


def test_successful_apply_disables_setup_but_keeps_dashboard_state(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    state = {
        "schema_version": 2,
        "hardware": {},
        "discovery": {"runtime_installed": True, "gguf_models": ["model.gguf"], "config_exists": True, "enabled_slots": 1},
        "platform_support": {"status": "supported"},
        "models": [],
        "setup_complete": True,
    }
    monkeypatch.setattr(SetupEngine, "apply", lambda _engine, _payload: {"ok": True, "state": state})
    monkeypatch.setattr(SetupEngine, "state", lambda _engine, **_kwargs: state)
    app = create_app(
        str(tmp_path / "missing.yaml"),
        setup_home=str(tmp_path / "home"),
        upstreams_path=str(tmp_path / "upstreams.yaml"),
        apps_path=str(tmp_path / "apps.yaml"),
        harnesses_path=str(tmp_path / "harnesses.yaml"),
    )
    client = TestClient(app)
    headers = {"X-Setup-Token": app.state.setup_token}

    assert client.post("/setup/apply", headers=headers, json={}).status_code == 200
    assert client.get("/setup/state", headers=headers).status_code == 410
    status = client.get("/ui/status")

    assert status.status_code == 200
    assert status.json()["setup_api_active"] is False
    assert status.json()["setup"]["setup_complete"] is True


def test_setup_events_api_tracks_rollover_gap_resync_and_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    app = create_app(
        str(tmp_path / "missing.yaml"),
        setup_home=str(tmp_path / "home"),
        upstreams_path=str(tmp_path / "upstreams.yaml"),
        apps_path=str(tmp_path / "apps.yaml"),
        harnesses_path=str(tmp_path / "harnesses.yaml"),
    )
    client = TestClient(app)
    headers = {"X-Setup-Token": app.state.setup_token}

    empty = client.get("/setup/events?after=0", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["history"] == "empty"
    assert empty.json()["resync_required"] is False

    for idx in range(1, 203):
        app.state.setup_engine._emit("download", "running", f"chunk-{idx}", progress=0.5)

    caught_up = client.get("/setup/events?after=202", headers=headers)
    assert caught_up.status_code == 200
    assert caught_up.json()["history"] == "retained"
    assert caught_up.json()["events"] == []

    app.state.setup_engine._emit("download", "complete", "chunk-203", progress=1.0)
    next_poll = client.get("/setup/events?after=202", headers=headers)
    assert next_poll.status_code == 200
    assert [ev["id"] for ev in next_poll.json()["events"]] == [203]

    gap = client.get("/setup/events?after=1", headers=headers)
    assert gap.status_code == 200
    assert gap.json()["history"] == "lost"
    assert gap.json()["resync_required"] is True
    assert gap.json()["cursor"] == 203
    assert len(gap.json()["events"]) == 200

    monkeypatch.setattr(app.state.setup_engine, "events", lambda _after=0: (_ for _ in ()).throw(RuntimeError("boom")))
    unavailable = client.get("/setup/events?after=0", headers=headers)
    assert unavailable.status_code == 503
    assert unavailable.json()["error"] == "setup_events_unavailable"
    assert unavailable.json()["history"] == "unavailable"
    assert unavailable.json()["resync_required"] is True


def test_setup_events_sse_resync_transition_and_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    app = create_app(
        str(tmp_path / "missing.yaml"),
        setup_home=str(tmp_path / "home"),
        upstreams_path=str(tmp_path / "upstreams.yaml"),
        apps_path=str(tmp_path / "apps.yaml"),
        harnesses_path=str(tmp_path / "harnesses.yaml"),
    )
    for idx in range(1, 206):
        app.state.setup_engine._emit("download", "running", f"step-{idx}", progress=0.5)

    class FakeRequest:
        def __init__(self, after: str, polls: int, *, on_sleep=None):
            self.query_params = {"after": after}
            self.headers = {"accept": "text/event-stream"}
            self._checks = 0
            self._polls = polls
            self._on_sleep = on_sleep

        async def is_disconnected(self) -> bool:
            # Two checks per loop iteration (top + bottom)
            disconnected = (self._checks // 2) >= self._polls
            self._checks += 1
            return disconnected

    async def run_resync_and_transition() -> list[str]:
        polls = 0

        async def fake_sleep(_delay: float) -> None:
            nonlocal polls
            polls += 1
            if polls == 1:
                app.state.setup_engine._emit("download", "complete", "step-206", progress=1.0)

        monkeypatch.setattr(routes_setup.asyncio, "sleep", fake_sleep)
        ctx = SimpleNamespace(setup_engine=app.state.setup_engine)
        req = FakeRequest(after="1", polls=2)
        response = await routes_setup.setup_events(ctx, req)
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return chunks

    chunks = asyncio.run(run_resync_and_transition())
    assert chunks[0].startswith("event: resync\n")
    resync_data = json.loads(chunks[0].split("data: ", 1)[1])
    assert resync_data["history"] == "lost"
    assert resync_data["resync_required"] is True
    assert resync_data["cursor"] == 205
    emitted_ids = [
        json.loads(chunk.split("data: ", 1)[1])["id"]
        for chunk in chunks
        if chunk.startswith("event: setup\n")
    ]
    assert emitted_ids[0] == 6
    assert emitted_ids[-2:] == [205, 206]
    assert emitted_ids.count(206) == 1

    async def run_unavailable() -> list[str]:
        monkeypatch.setattr(app.state.setup_engine, "events", lambda _after=0: (_ for _ in ()).throw(RuntimeError("down")))
        ctx = SimpleNamespace(setup_engine=app.state.setup_engine)
        req = FakeRequest(after="0", polls=1)
        response = await routes_setup.setup_events(ctx, req)
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return chunks

    err_chunks = asyncio.run(run_unavailable())
    assert len(err_chunks) == 1
    assert err_chunks[0].startswith("event: resync\n")
    err_data = json.loads(err_chunks[0].split("data: ", 1)[1])
    assert err_data == {
        "error": "setup_events_unavailable",
        "history": "unavailable",
        "resync_required": True,
    }


def test_sse_disconnect_preserves_running_setup_and_first_stop_cause_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    app = create_app(
        str(tmp_path / "missing.yaml"),
        setup_home=str(tmp_path / "home"),
        upstreams_path=str(tmp_path / "upstreams.yaml"),
        apps_path=str(tmp_path / "apps.yaml"),
        harnesses_path=str(tmp_path / "harnesses.yaml"),
    )
    app.state.setup_engine._emit("download", "running", "in-progress", progress=0.4)

    class DisconnectingRequest:
        query_params = {"after": "0"}
        headers = {"accept": "text/event-stream"}

        def __init__(self):
            self._calls = 0

        async def is_disconnected(self) -> bool:
            self._calls += 1
            return self._calls > 1

    async def consume_then_disconnect() -> None:
        ctx = SimpleNamespace(setup_engine=app.state.setup_engine)
        response = await routes_setup.setup_events(ctx, DisconnectingRequest())
        async for _chunk in response.body_iterator:
            pass

    asyncio.run(consume_then_disconnect())
    state_after_disconnect = app.state.setup_engine.events(0)
    assert state_after_disconnect["cancelled"] is False
    assert state_after_disconnect["stop"] is None

    client = TestClient(app)
    headers = {"X-Setup-Token": app.state.setup_token, "X-Request-Id": "req-stop-1"}
    first = client.post(
        "/setup/cancel",
        headers=headers,
        json={"cause": "ui_cancel", "actor": "dashboard"},
    )
    assert first.status_code == 200
    assert first.json()["stop"]["cause"] == "ui_cancel"
    assert first.json()["stop"]["actor"] == "dashboard"
    assert first.json()["stop"]["request_id"] == "req-stop-1"
    assert first.json()["stop"]["state"] == "cancel_requested"

    second = client.post(
        "/setup/cancel",
        headers={"X-Setup-Token": app.state.setup_token, "X-Request-Id": "req-stop-2"},
        json={"cause": "late_override", "actor": "cli"},
    )
    assert second.status_code == 200
    assert second.json()["stop"]["cause"] == "ui_cancel"
    assert second.json()["stop"]["actor"] == "dashboard"
    assert second.json()["stop"]["request_id"] == "req-stop-1"

    shut_one = client.post(
        "/fleet/prepare-shutdown",
        headers={"X-Request-Id": "shut-1"},
        json={"cause": "cli_shutdown", "actor": "cli"},
    )
    shut_two = client.post(
        "/fleet/prepare-shutdown",
        headers={"X-Request-Id": "shut-2"},
        json={"cause": "other_cause", "actor": "other"},
    )
    assert shut_one.status_code == 200
    assert shut_one.json()["stop"]["cause"] == "cli_shutdown"
    assert shut_two.json()["stop"]["cause"] == "cli_shutdown"
    assert shut_two.json()["stop"]["request_id"] == "shut-1"
