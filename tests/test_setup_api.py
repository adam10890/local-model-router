from __future__ import annotations

from starlette.testclient import TestClient

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
