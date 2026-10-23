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


def test_shutdown_drains_before_terminating_verified_router(tmp_path, monkeypatch, capsys):
    import local_model_router.service.__main__ as service_main

    state_path = tmp_path / "router-process.json"
    state_path.write_text(
        json.dumps(
            {
                "pid": 4321,
                "process_executable": "python.exe",
                "process_created_at": 12.5,
                "port": 9012,
            }
        ),
        encoding="utf-8",
    )
    events: list[str] = []

    class Process:
        pid = 4321

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout):
            events.append(f"wait:{timeout}")

    class Response:
        status = 200

        def read(self):
            events.append("drain")
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(service_main, "router_process_state_path", lambda: state_path)
    monkeypatch.setattr(cli, "_verified_router_process", lambda _state: Process())
    timeouts = []

    def urlopen(request, *, timeout):
        timeouts.append(timeout)
        return Response()

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    assert cli.cmd_shutdown(SimpleNamespace()) == 0
    assert events == ["drain", "terminate", "wait:10"]
    assert timeouts == [cli._SHUTDOWN_DRAIN_TIMEOUT_SECONDS]
    assert not state_path.exists()
    assert json.loads(capsys.readouterr().out)["stopped"] is True


def test_shutdown_refuses_live_router_without_ownership_state(tmp_path, monkeypatch, capsys):
    import local_model_router.service.__main__ as service_main

    monkeypatch.setattr(
        service_main, "router_process_state_path", lambda: tmp_path / "missing.json"
    )
    monkeypatch.setattr(cli, "_probe", lambda _url: True)

    assert cli.cmd_shutdown(SimpleNamespace()) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "router_ownership_unverified"


def test_verified_router_process_checks_pid_creation_executable_and_command(
    tmp_path, monkeypatch
):
    executable = tmp_path / "python.exe"
    executable.touch()
    other_executable = tmp_path / "other-python.exe"
    other_executable.touch()
    actual = {
        "created": 12.5,
        "executable": str(executable),
        "cmdline": [str(executable), "-m", "local_model_router", "serve"],
    }

    class Process:
        pid = 4321

        def __init__(self, pid):
            assert pid == 4321

        def exe(self):
            return actual["executable"]

        def create_time(self):
            return actual["created"]

        def cmdline(self):
            return actual["cmdline"]

    monkeypatch.setattr("psutil.Process", Process)
    state = {
        "pid": 4321,
        "process_executable": str(executable),
        "process_created_at": 12.5,
    }

    verified = cli._verified_router_process(state)
    assert verified.pid == 4321

    for accepted in (
        [str(executable), "-m", "local_model_router"],
        [str(executable), "-m", "local_model_router.service"],
        [str(executable), "-m", "local_model_router.service.__main__"],
    ):
        actual["cmdline"] = accepted
        assert cli._verified_router_process(state).pid == 4321

    for script_name in ("imperium.exe", "local-model-router.exe"):
        script = tmp_path / script_name
        script.touch()
        console_state = {**state, "process_executable": str(script)}
        actual.update(executable=str(script), cmdline=[str(script), "serve"])
        assert cli._verified_router_process(console_state).pid == 4321
        actual["cmdline"] = [str(script)]
        assert cli._verified_router_process(console_state).pid == 4321
        actual["cmdline"] = [str(script), "doctor"]
        with pytest.raises(RuntimeError, match="router_ownership_unverified"):
            cli._verified_router_process(console_state)

    actual.update(
        executable=str(executable),
        cmdline=[str(executable), "-m", "local_model_router", "serve"],
    )

    actual["cmdline"] = []
    with pytest.raises(RuntimeError, match="router_ownership_unverified"):
        cli._verified_router_process(state)
    actual["cmdline"] = [str(executable), "-m", "local_model_router", "serve"]

    with pytest.raises(RuntimeError, match="router_ownership_unverified"):
        cli._verified_router_process({**state, "pid": 0})

    actual["created"] = 13.0
    with pytest.raises(RuntimeError, match="router_ownership_unverified"):
        cli._verified_router_process(state)

    actual.update(created=12.5, executable=str(other_executable))
    with pytest.raises(RuntimeError, match="router_ownership_unverified"):
        cli._verified_router_process(state)

    actual.update(
        executable=str(executable),
        cmdline=[str(executable), "-m", "local_model_router", "status"],
    )
    with pytest.raises(RuntimeError, match="router_ownership_unverified"):
        cli._verified_router_process(state)


def test_shutdown_timeout_keeps_verified_router_and_state(tmp_path, monkeypatch, capsys):
    import local_model_router.service.__main__ as service_main

    state_path = tmp_path / "router-process.json"
    state_path.write_text(
        json.dumps(
            {
                "pid": 4321,
                "process_executable": "python.exe",
                "process_created_at": 12.5,
                "port": 9012,
            }
        ),
        encoding="utf-8",
    )

    class Process:
        pid = 4321

        def terminate(self):
            raise AssertionError("router must remain running after drain timeout")

    def timeout(_request, *, timeout):
        assert timeout == cli._SHUTDOWN_DRAIN_TIMEOUT_SECONDS
        raise TimeoutError

    monkeypatch.setattr(service_main, "router_process_state_path", lambda: state_path)
    monkeypatch.setattr(cli, "_verified_router_process", lambda _state: Process())
    monkeypatch.setattr(cli.urllib.request, "urlopen", timeout)

    assert cli.cmd_shutdown(SimpleNamespace()) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "router_drain_timeout"
    assert state_path.exists()


def test_shutdown_accepts_router_exit_during_successful_drain(
    tmp_path, monkeypatch, capsys
):
    import psutil
    import local_model_router.service.__main__ as service_main

    state_path = tmp_path / "router-process.json"
    state_path.write_text(
        json.dumps(
            {
                "pid": 4321,
                "process_executable": "python.exe",
                "process_created_at": 12.5,
                "port": 9012,
            }
        ),
        encoding="utf-8",
    )

    class Process:
        pid = 4321

        def terminate(self):
            raise psutil.NoSuchProcess(self.pid)

    class Response:
        status = 200

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(service_main, "router_process_state_path", lambda: state_path)
    monkeypatch.setattr(cli, "_verified_router_process", lambda _state: Process())
    monkeypatch.setattr(
        cli.urllib.request,
        "urlopen",
        lambda _request, *, timeout: Response(),
    )

    assert cli.cmd_shutdown(SimpleNamespace()) == 0
    assert json.loads(capsys.readouterr().out)["stopped"] is True
    assert not state_path.exists()


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
