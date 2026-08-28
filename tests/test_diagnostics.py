from __future__ import annotations

import importlib
import json
import types

from starlette.testclient import TestClient

from local_model_router.diagnostics import build_diagnostics_report, collect_doctor_checks


def _config(tmp_path, body: str = "active_slots: []\nglobal:\n  backend: remote\n"):
    path = tmp_path / "llama_cpp_servers.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _setup_state(*_args, **_kwargs):
    return {
        "recommended_backend": "cpu",
        "platform_support": {"status": "supported"},
        "discovery": {
            "runtime_installed": True,
            "gguf_models": [{"id": "model"}],
            "config_exists": True,
            "enabled_slots": 1,
        },
        "hardware": {},
    }


class _Snapshot:
    def vram_summary(self):
        return {"source": "test", "total_gb": 8, "used_gb": 2, "available_gb": 6}

    def to_dict(self):
        return {
            "available": True,
            "timestamp": 1,
            "cpu": {"utilization_pct": 12.5},
            "ram": {"total_mb": 16384, "used_mb": 4096, "available_mb": 12288},
            "gpus": [
                {
                    "id": 0,
                    "name": "secret-gpu-name",
                    "total_vram_mb": 8192,
                    "used_vram_mb": 2048,
                    "free_vram_mb": 6144,
                    "utilization_pct": 25,
                    "temperature_c": 50,
                }
            ],
        }


def test_doctor_collector_covers_valid_missing_and_broken_dependency(tmp_path):
    config = _config(tmp_path)
    healthy = collect_doctor_checks(str(config), probe=lambda _url: True)
    assert healthy["ok"] is True
    assert next(row for row in healthy["checks"] if row["code"] == "dependency_aiohttp")["status"] == "pass"

    missing = collect_doctor_checks(str(tmp_path / "missing.yaml"), probe=lambda _url: True)
    assert missing["ok"] is False
    assert next(row for row in missing["checks"] if row["code"] == "config_file_exists")["status"] == "fail"

    real_import = importlib.import_module

    def broken_import(name):
        return types.ModuleType("aiohttp") if name == "aiohttp" else real_import(name)

    broken = collect_doctor_checks(str(config), probe=lambda _url: True, import_module=broken_import)
    check = next(row for row in broken["checks"] if row["code"] == "dependency_aiohttp")
    assert broken["ok"] is False
    assert check["detail"] == "required capability unavailable: aiohttp.ClientSession"
    assert check["remediation"] == "Reinstall aiohttp in the Imperium Python environment"


def test_doctor_collector_sanitizes_import_and_malformed_config_failures(tmp_path):
    config = _config(tmp_path, "active_slots: [broken\nsecret: C:\\private\\key.txt\n")
    real_import = importlib.import_module

    def failing_import(name):
        if name == "aiohttp":
            raise ImportError("C:\\private\\site-packages secret-token")
        return real_import(name)

    report = collect_doctor_checks(
        str(config),
        probe=lambda _url: False,
        import_module=failing_import,
        include_locations=False,
    )
    serialized = json.dumps(report)
    assert report["ok"] is False
    assert next(row for row in report["checks"] if row["code"] == "config_parses")["detail"] == "configuration could not be parsed"
    assert "private" not in serialized.lower()
    assert "secret-token" not in serialized


def test_doctor_collector_reports_reachable_and_unreachable_slots(tmp_path):
    config = _config(
        tmp_path,
        """active_slots:
  - id: chat
    host: 127.0.0.1
    port: 8080
    role: chat
    enabled: true
global:
  backend: subprocess
""",
    )
    reachable = collect_doctor_checks(
        str(config), probe=lambda _url: True, include_locations=False
    )
    assert reachable["summary"] == {"enabled_slots": 1, "reachable_slots": 1}
    assert next(row for row in reachable["checks"] if row["code"] == "slot_reachable_chat")["detail"] == "slot responded"

    unreachable = collect_doctor_checks(
        str(config), probe=lambda _url: False, include_locations=False
    )
    assert unreachable["summary"] == {"enabled_slots": 1, "reachable_slots": 0}
    assert unreachable["ok"] is False
    assert next(row for row in unreachable["checks"] if row["code"] == "slot_reachable_chat")["detail"] == "slot did not respond"


def test_report_builder_only_emits_allowlisted_operational_fields():
    secret = "never-export-this"
    report = build_diagnostics_report(
        generated_at="2026-08-28T12:00:00Z",
        imperium_version="0.11.0",
        doctor={"ok": True, "checks": []},
        readiness={
            "overall": "ready",
            "blocking_issues": [],
            "optional_issues": [],
            "next_action": {"code": "start_chat", "href": "#/chat", "label": {"en": "Chat", "he": "צ׳אט"}},
            "prompt": secret,
            "api": {"base_url": f"http://user:{secret}@localhost"},
        },
        slots=[
            {
                "id": "chat",
                "role": "chat",
                "enabled": True,
                "backend_type": "subprocess",
                "health": "unhealthy",
                "model_path": f"C:\\private\\{secret}.gguf",
                "response": secret,
            }
        ],
        hardware={
            "available": True,
            "cpu": {"utilization_pct": 10},
            "ram": {"total_mb": 100, "available_mb": 50},
            "gpus": [{"id": 0, "name": secret, "total_vram_mb": 80}],
        },
        backend="subprocess",
        fleet_control_enabled=False,
        fleet_control_supported=True,
        auth_enabled=True,
    )
    serialized = json.dumps(report)
    assert secret not in serialized
    assert "C:\\\\private" not in serialized
    assert "user:" not in serialized
    assert report["slots"][0]["runtime"]["failure_code"] == "health_probe_failed"
    assert report["hardware"]["gpus"] == [
        {
            "id": 0,
            "total_vram_mb": 80,
            "used_vram_mb": None,
            "free_vram_mb": None,
            "utilization_pct": None,
            "temperature_c": None,
        }
    ]


def _client(tmp_path, monkeypatch, *, secret_config: bool = False):
    import local_model_router.service.app as app_module

    body = "active_slots: []\nglobal:\n  backend: remote\n"
    if secret_config:
        body = """active_slots:
  - id: chat
    host: 127.0.0.1
    port: 9999
    role: chat
    enabled: false
    model_path: C:\\private\\never-export-this.gguf
    prompt: never-export-this
global:
  backend: remote
  api_key: never-export-this
  callback_url: http://user:never-export-this@example.invalid/path
"""
    config = _config(tmp_path, body)
    monkeypatch.setattr(app_module.SetupEngine, "state", _setup_state)
    monkeypatch.setattr(app_module, "scan_hardware", lambda: _Snapshot())
    app = app_module.create_app(
        str(config),
        setup_home=str(tmp_path / "home"),
        upstreams_path=str(tmp_path / "upstreams.yaml"),
        apps_path=str(tmp_path / "apps.yaml"),
        harnesses_path=str(tmp_path / "harnesses.yaml"),
    )
    return TestClient(app), config


def test_diagnostics_endpoint_is_authenticated_and_has_stable_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", "local-api-key")
    client, _config_path = _client(tmp_path, monkeypatch)

    assert client.get("/diagnostics/report").status_code == 401
    response = client.get(
        "/diagnostics/report", headers={"Authorization": "Bearer local-api-key"}
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "schema_version",
        "generated_at",
        "imperium_version",
        "ok",
        "readiness",
        "doctor",
        "slots",
        "hardware",
        "runtime",
        "collection_errors",
    }
    assert body["schema_version"] == 1
    assert body["generated_at"].endswith("Z")
    assert body["runtime"]["auth_enabled"] is True


def test_diagnostics_endpoint_excludes_config_secrets_prompts_urls_and_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    client, _config_path = _client(tmp_path, monkeypatch, secret_config=True)

    response = client.get("/diagnostics/report")
    serialized = response.text.lower()
    assert response.status_code == 200
    assert "never-export-this" not in serialized
    assert "private" not in serialized
    assert "user:" not in serialized
    assert "prompt" not in serialized
    assert "authorization" not in serialized


def test_diagnostics_endpoint_returns_200_and_codes_partial_collection_failures(tmp_path, monkeypatch):
    import local_model_router.service.app as app_module

    config = _config(tmp_path)

    def fail(*_args, **_kwargs):
        raise RuntimeError("C:\\private\\never-export-this prompt body")

    async def fail_async(*_args, **_kwargs):
        raise RuntimeError("C:\\private\\never-export-this prompt body")

    monkeypatch.setattr(app_module, "collect_doctor_checks", fail)
    monkeypatch.setattr(app_module.SetupEngine, "state", fail)
    monkeypatch.setattr(app_module.ObserverBackend, "get_slots_health", fail_async)
    monkeypatch.setattr(app_module, "scan_hardware", fail)
    client = TestClient(
        app_module.create_app(
            str(config),
            setup_home=str(tmp_path / "home"),
            upstreams_path=str(tmp_path / "upstreams.yaml"),
            apps_path=str(tmp_path / "apps.yaml"),
            harnesses_path=str(tmp_path / "harnesses.yaml"),
        )
    )

    response = client.get("/diagnostics/report")
    body = response.json()
    codes = {row["code"] for row in body["collection_errors"]}
    assert response.status_code == 200
    assert body["ok"] is False
    assert codes == {
        "doctor_collection_failed",
        "setup_state_unavailable",
        "slots_health_unavailable",
        "hardware_unavailable",
    }
    assert "never-export-this" not in response.text
    assert "private" not in response.text.lower()


def test_diagnostics_endpoint_reports_config_that_breaks_after_startup(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    client, config = _client(tmp_path, monkeypatch)
    config.write_text("active_slots: [broken\nsecret: never-export-this", encoding="utf-8")

    response = client.get("/diagnostics/report")
    check = next(
        row for row in response.json()["doctor"]["checks"] if row["code"] == "config_parses"
    )
    assert response.status_code == 200
    assert check["status"] == "fail"
    assert check["detail"] == "configuration could not be parsed"
    assert "never-export-this" not in response.text
