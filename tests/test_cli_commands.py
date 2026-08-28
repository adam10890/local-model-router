"""Hermetic behavior coverage for CLI command handlers."""
from __future__ import annotations

import builtins
import json
import urllib.error
from types import SimpleNamespace

import pytest

from local_model_router import cli
from local_model_router.setup import SetupError


def test_probe_maps_http_status_and_network_failure(monkeypatch):
    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert cli._probe("http://router.test/health") is True

    monkeypatch.setattr(
        cli.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )
    assert cli._probe("http://router.test/health") is False


def test_serve_and_mcp_commands_delegate_or_explain_missing_extra(monkeypatch, capsys):
    import local_model_router.mcp.server as mcp_server
    import local_model_router.service.__main__ as service_main

    calls = []
    monkeypatch.setattr(service_main, "main", lambda: calls.append("serve"))
    monkeypatch.setattr(mcp_server, "main", lambda: calls.append("mcp"))
    assert cli.main(["serve"]) == 0
    assert cli.main(["mcp"]) == 0
    assert calls == ["serve", "mcp"]

    real_import = builtins.__import__

    def import_without_mcp(name, *args, **kwargs):
        if name == "local_model_router.mcp.server":
            raise ImportError("optional extra absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_mcp)
    assert cli.cmd_mcp(SimpleNamespace()) == 1
    assert "MCP support is not installed" in capsys.readouterr().out


def test_config_check_missing_non_mapping_and_empty_config(tmp_path, monkeypatch, capsys):
    config = tmp_path / "fleet.yaml"
    monkeypatch.setattr(cli, "_resolve_config", lambda: str(config))
    assert cli.main(["config-check"]) == 1
    assert "config file not found" in capsys.readouterr().out

    config.write_text("- not\n- a mapping\n", encoding="utf-8")
    assert cli.main(["config-check"]) == 1
    assert "top level" in capsys.readouterr().out

    config.write_text("active_slots: []\n", encoding="utf-8")
    assert cli.main(["config-check"]) == 0
    assert "no enabled slots" in capsys.readouterr().out


def test_list_models_formats_aliases_and_live_models(monkeypatch, capsys):
    import local_model_router.service.models_listing as models_listing
    import local_model_router.service.observer as observer

    class Observer:
        def __init__(self, path):
            assert path == "fleet.yaml"

    async def listing(_observer):
        return {
            "data": [
                {
                    "id": "chat",
                    "meta": {
                        "kind": "alias",
                        "maps_to_role": "chat",
                        "live": {"slot_id": "slot-chat", "n_ctx": 8192},
                    },
                },
                {"id": "model-id", "meta": {"slot_id": "slot-chat", "n_ctx": 4096}},
            ]
        }

    monkeypatch.setattr(cli, "_resolve_config", lambda: "fleet.yaml")
    monkeypatch.setattr(observer, "ObserverBackend", Observer)
    monkeypatch.setattr(models_listing, "list_models", listing)

    assert cli.main(["list-models"]) == 0
    output = capsys.readouterr().out
    assert "alias  chat" in output and "slot=slot-chat n_ctx=8192" in output
    assert "model  model-id" in output and "n_ctx=4096" in output


@pytest.mark.parametrize(
    ("result", "error", "expected"),
    [({"models": [{"id": "m"}]}, None, 0), ({"models": []}, None, 2), (None, "failed", 2)],
)
def test_evaluate_models_exit_codes(monkeypatch, capsys, result, error, expected):
    import local_model_router.evaluation as evaluation
    import local_model_router.service.fleet_manager as fleet_manager

    monkeypatch.setattr(evaluation, "http_requester", lambda base, key: (base, key))
    monkeypatch.setattr(fleet_manager, "FleetStore", lambda: object())

    def evaluate(*_args, **_kwargs):
        if error:
            raise RuntimeError(error)
        return result

    monkeypatch.setattr(evaluation, "evaluate_models", evaluate)
    rc = cli.main(["evaluate-models", "--base-url", "http://router.test", "--force"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == expected
    assert payload == ({"error": "evaluation_failed", "detail": error} if error else result)


class _SetupEngine:
    config_path = "fleet.yaml"
    home = "home"

    def __init__(self):
        self.calls = []
        self.update = {"update_available": False}
        self.runtime = {"backend": "cpu"}

    def state(self, refresh_hardware=False):
        self.calls.append(("state", refresh_hardware))
        return {
            "discovery": {
                "runtime_installed": False,
                "gguf_models": [],
                "config_exists": False,
            }
        }

    def start_managed(self, visible_terminal=False):
        self.calls.append(("start", visible_terminal))
        return {"running": True}

    def stop_managed(self):
        self.calls.append(("stop",))
        return {"running": False}

    def repair(self, *, confirm=False):
        self.calls.append(("repair", confirm))
        if confirm:
            return {"ok": True, "missing": ["configuration"], "repaired": True}
        return {
            "ok": False,
            "missing": ["runtime", "model", "configuration"],
            "confirmation_required": True,
        }

    def update_status(self):
        return self.update

    def _managed_runtime(self):
        return self.runtime

    def install_runtime(self, backend, channel):
        self.calls.append(("install", backend, channel))
        return {"backend": backend, "channel": channel}

    def rollback(self):
        self.calls.append(("rollback",))
        return {"version": "previous"}


def test_setup_status_repair_start_and_stop(monkeypatch, capsys):
    engine = _SetupEngine()
    monkeypatch.setattr(cli, "_setup_engine", lambda: engine)

    assert cli.main(["setup", "--status"]) == 0
    assert cli.main(["setup", "--repair"]) == 1
    assert cli.main(["setup", "--repair", "--yes"]) == 0
    assert cli.main(["setup", "--start-runtime", "--terminal"]) == 0
    assert cli.main(["setup", "--stop-runtime"]) == 0

    assert ("state", False) in engine.calls
    assert ("repair", False) in engine.calls
    assert ("repair", True) in engine.calls
    assert ("start", True) in engine.calls
    assert ("stop",) in engine.calls
    assert '"missing": [' in capsys.readouterr().out


def test_setup_rejects_non_object_plan(tmp_path, monkeypatch, capsys):
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(cli, "_setup_engine", _SetupEngine)

    assert cli.main(["setup", "--plan", str(plan), "--yes"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_plan"


def test_update_confirmation_install_missing_runtime_and_rollback(monkeypatch, capsys):
    engine = _SetupEngine()
    monkeypatch.setattr(cli, "_setup_engine", lambda: engine)

    assert cli.main(["update", "--check"]) == 0
    engine.update = {"update_available": True, "latest": "next"}
    assert cli.main(["update"]) == 2
    assert cli.main(["update", "--yes"]) == 0
    assert ("install", "cpu", "latest") in engine.calls

    capsys.readouterr()
    engine.runtime = None
    assert cli.main(["update", "--yes"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "runtime_missing"

    assert cli.main(["rollback"]) == 0
    assert ("rollback",) in engine.calls


def test_rollback_maps_setup_error(monkeypatch, capsys):
    class Engine:
        def rollback(self):
            raise SetupError("rollback_unavailable", "No previous runtime")

    monkeypatch.setattr(cli, "_setup_engine", Engine)
    assert cli.main(["rollback"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "rollback_unavailable"
