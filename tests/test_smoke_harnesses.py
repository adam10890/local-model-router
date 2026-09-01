from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
import json
from pathlib import Path
import subprocess
import urllib.error

import pytest


def _script_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "smoke_harnesses.py"
    spec = spec_from_file_location("smoke_harnesses_extra", script)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_harness_smoke_checks_models_completion_and_stream(monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "smoke_harnesses.py"
    assert script.exists()
    spec = spec_from_file_location("smoke_harnesses", script)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def request(method, url, api_key, payload=None, timeout=180, stream=False):
        calls.append((method, url, payload, stream))
        if url.endswith("/harnesses"):
            return {
                "harnesses": [{
                    "harness_id": "hermes",
                    "connections": [{"name": "default"}],
                }]
            }
        return {"choices": [{"message": {"content": "OK"}}]}

    monkeypatch.setattr(module, "_request", request)
    module.smoke("http://router:9000", "secret")

    assert [call[:2] for call in calls] == [
        ("GET", "http://router:9000/harnesses"),
        ("GET", "http://router:9000/harnesses/hermes/v1/models"),
        ("POST", "http://router:9000/harnesses/hermes/v1/chat/completions"),
        ("POST", "http://router:9000/harnesses/hermes/v1/chat/completions"),
    ]
    assert "stream" not in calls[2][2]
    assert calls[3][2]["stream"] is True
    assert calls[3][3] is True


def test_harness_smoke_can_skip_stream_and_add_tools(monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "smoke_harnesses.py"
    spec = spec_from_file_location("smoke_harnesses", script)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def request(method, url, api_key, payload=None, timeout=180, stream=False):
        calls.append((method, url, payload, stream))
        if url.endswith("/harnesses"):
            return {
                "harnesses": [{
                    "harness_id": "pi",
                    "connections": [{"name": "default"}],
                }]
            }
        if payload and payload.get("tools"):
            return {
                "choices": [{"message": {"tool_calls": [{
                    "function": {"name": "imperium_ping", "arguments": "{}"}
                }]}}]
            }
        return {"choices": [{"message": {"content": "OK"}}]}

    monkeypatch.setattr(module, "_request", request)
    module.smoke("http://router:9000", check_stream=False, check_tools=True)

    assert len(calls) == 4
    assert calls[1][1] == "http://router:9000/harnesses/pi/v1/models"
    assert calls[3][2]["tools"][0]["function"]["name"] == "imperium_ping"
    assert calls[3][2]["tool_choice"] == "required"
    assert calls[3][2]["chat_template_kwargs"] == {"enable_thinking": False}
    assert calls[3][2]["temperature"] == 0


def test_harness_smoke_filter_and_named_connection(monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "smoke_harnesses.py"
    spec = spec_from_file_location("smoke_harnesses", script)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def request(method, url, api_key, payload=None, timeout=180, stream=False):
        calls.append((method, url))
        if url.endswith("/harnesses"):
            return {
                "harnesses": [
                    {"harness_id": "hermes", "connections": [{"name": "default"}]},
                    {
                        "harness_id": "agent_zero",
                        "connections": [{"name": "chat"}, {"name": "utility"}],
                    },
                ]
            }
        return {"choices": [{"message": {"content": "OK"}}]}

    monkeypatch.setattr(module, "_request", request)
    module.smoke(
        "http://router:9000",
        check_stream=False,
        harness_ids={"agent_zero"},
    )
    assert [url for _, url in calls] == [
        "http://router:9000/harnesses",
        "http://router:9000/harnesses/agent_zero/chat/v1/models",
        "http://router:9000/harnesses/agent_zero/chat/v1/chat/completions",
        "http://router:9000/harnesses/agent_zero/utility/v1/models",
        "http://router:9000/harnesses/agent_zero/utility/v1/chat/completions",
    ]


def test_harness_smoke_required_missing_writes_sanitized_json(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "smoke_harnesses.py"
    spec = spec_from_file_location("smoke_harnesses", script)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "_request",
        lambda *_args, **_kwargs: {
            "harnesses": [{"harness_id": "hermes", "connections": [{"name": "default"}]}]
        },
    )
    output = tmp_path / "harness.json"

    with pytest.raises(RuntimeError, match="required_harness_missing"):
        module.smoke(
            "http://router:9000",
            api_key="do-not-write",
            harness_ids={"pi"},
            json_output=output,
        )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["missing_harnesses"] == ["pi"]
    assert report["error_code"] == "required_harness_missing"
    serialized = json.dumps(report)
    assert "do-not-write" not in serialized
    assert "router:9000" not in serialized


@pytest.mark.parametrize(
    ("kind", "output", "version"),
    [
        ("hermes", "Hermes Agent v0.21.0 (2026.8.31)\nInstall directory: C:\\private", "0.21.0"),
        ("agent_zero", "2.11\n", "2.11"),
        ("pi", "0.80.6\n", "0.80.6"),
        ("claude_code", "2.1.220 (Claude Code)\n", "2.1.220"),
    ],
)
def test_installed_version_probe_keeps_only_parsed_version(kind, output, version):
    module = _script_module()

    result = module._probe_installed(
        kind,
        which=lambda _name: "C:\\private\\client.cmd",
        run=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, output, "secret stderr"),
    )

    assert result == {"status": "pass", "evidence": "observed", "version": version}
    assert "private" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


def test_installed_version_probe_reports_missing_timeout_failure_and_unknown_output():
    module = _script_module()
    assert module._probe_installed("hermes", which=lambda _name: None)["reason_code"] == "executable_not_found"

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("hermes", 5)

    assert module._probe_installed("hermes", which=lambda _name: "hermes", run=timeout)["reason_code"] == "probe_timeout"
    failed = lambda *_args, **_kwargs: subprocess.CompletedProcess([], 2, "", "private failure")
    assert module._probe_installed("hermes", which=lambda _name: "hermes", run=failed)["reason_code"] == "probe_failed"
    unknown = lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "future format", "")
    assert module._probe_installed("hermes", which=lambda _name: "hermes", run=unknown)["reason_code"] == "version_unrecognized"


@pytest.mark.parametrize(
    ("kind", "payload", "version"),
    [
        ("hermes", {"name": "Hermes Agent v0.21.0 (v2026.8.31)"}, "0.21.0"),
        ("agent_zero", {"tag_name": "v2.11"}, "2.11"),
        ("pi", {"version": "0.84.4"}, "0.84.4"),
        ("claude_code", {"version": "2.1.257"}, "2.1.257"),
    ],
)
def test_stable_version_probe_uses_bounded_official_fields(kind, payload, version):
    module = _script_module()
    opener = lambda *_args, **_kwargs: _Response(json.dumps(payload).encode())

    result = module._probe_stable(kind, opener=opener)

    assert result["status"] == "pass"
    assert result["evidence"] == "documented"
    assert result["version"] == version
    assert result["source_url"].startswith("https://")


def test_stable_version_probe_keeps_offline_and_malformed_results_unknown():
    module = _script_module()

    def offline(*_args, **_kwargs):
        raise urllib.error.URLError("private network detail")

    assert module._probe_stable("hermes", opener=offline)["reason_code"] == "stable_lookup_failed"
    malformed = lambda *_args, **_kwargs: _Response(b"{not-json")
    assert module._probe_stable("hermes", opener=malformed)["reason_code"] == "stable_lookup_failed"
    missing = lambda *_args, **_kwargs: _Response(b"{}")
    assert module._probe_stable("hermes", opener=missing)["reason_code"] == "stable_version_unrecognized"


@pytest.mark.parametrize(
    ("installed", "stable", "alignment"),
    [
        ("0.21.0", "0.21.0", "current"),
        ("0.80.6", "0.84.4", "behind"),
        ("2.2.0", "2.1.257", "ahead"),
    ],
)
def test_stable_version_alignment_compares_installed_and_official(installed, stable, alignment):
    module = _script_module()

    result = module._stable_with_alignment(
        {"status": "pass", "version": installed},
        {"status": "pass", "evidence": "documented", "version": stable},
    )

    assert result["alignment"] == alignment
    assert result["installed_version"] == installed
    assert result["version"] == stable


def test_hermes_canary_is_isolated_tool_free_and_sanitized(monkeypatch):
    module = _script_module()
    monkeypatch.setenv("OPENAI_API_KEY", "cloud-secret")
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "cloud-token")
    observed = {}

    def run(args, **kwargs):
        observed.update({"args": args, **kwargs})
        config = Path(kwargs["env"]["HERMES_HOME"], "config.yaml").read_text()
        assert "base_url: http://127.0.0.1:9001/harnesses/hermes/v1" in config
        assert "http://127.0.0.1:9000" not in config
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(
            args, 0, "Hermes banner\nIMPERIUM_CANARY_OK\n", "private stderr"
        )

    counts = iter([4, 5])

    result = module._run_hermes_canary(
        "model:\n  base_url: http://127.0.0.1:9000/harnesses/hermes/v1\n",
        "http://127.0.0.1:9001/harnesses/hermes/v1",
        "http://127.0.0.1:9001",
        "router-secret",
        30,
        which=lambda _name: "C:\\private\\hermes.cmd",
        run=run,
        request_count=lambda *_args: next(counts),
    )

    assert result == {"status": "pass", "evidence": "tested", "routing": "verified"}
    assert observed["args"][1:3] == ["-z", module._HERMES_CANARY_PROMPT]
    assert observed["args"][observed["args"].index("-t") + 1] == module._HERMES_CANARY_TOOLSET
    assert "--ignore-user-config" in observed["args"]
    assert "OPENAI_API_KEY" not in observed["env"]
    assert "ANTHROPIC_OAUTH_TOKEN" not in observed["env"]
    assert observed["env"]["ROUTER_API_KEY"] == "router-secret"
    assert not Path(observed["cwd"]).exists()
    assert "router-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    ("runner", "reason"),
    [
        (lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, "wrong", ""), "unexpected_response"),
        (lambda args, **_kwargs: subprocess.CompletedProcess(args, 1, "", "private"), "client_canary_failed"),
        (lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("hermes", 1)), "client_canary_timeout"),
    ],
)
def test_hermes_canary_failure_modes_remain_content_free(runner, reason):
    module = _script_module()
    result = module._run_hermes_canary(
        "model:\n  base_url: http://127.0.0.1:9000/harnesses/hermes/v1\n",
        "http://127.0.0.1:9000/harnesses/hermes/v1",
        "http://127.0.0.1:9000",
        "router-secret",
        1,
        which=lambda _name: "hermes",
        run=runner,
        request_count=lambda *_args: 4,
    )
    assert result["reason_code"] == reason
    assert "router-secret" not in json.dumps(result)
    assert "wrong" not in json.dumps(result)
    assert "private" not in json.dumps(result)


def test_hermes_canary_refuses_missing_base_url_without_running_client():
    module = _script_module()

    result = module._run_hermes_canary(
        "model: {}\n",
        "http://127.0.0.1:9000/harnesses/hermes/v1",
        "http://127.0.0.1:9000",
        "router-secret",
        1,
        which=lambda _name: "hermes",
        run=lambda *_args, **_kwargs: pytest.fail("client must not run"),
        request_count=lambda *_args: pytest.fail("counter must not run"),
    )

    assert result["reason_code"] == "setup_base_url_missing"


def test_hermes_canary_keeps_success_unknown_when_routing_is_unverified():
    module = _script_module()

    result = module._run_hermes_canary(
        "model:\n  base_url: http://127.0.0.1:9000/harnesses/hermes/v1\n",
        "http://127.0.0.1:9000/harnesses/hermes/v1",
        "http://127.0.0.1:9000",
        "router-secret",
        1,
        which=lambda _name: "hermes",
        run=lambda args, **_kwargs: subprocess.CompletedProcess(
            args, 0, "IMPERIUM_CANARY_OK", ""
        ),
        request_count=lambda *_args: None,
    )

    assert result == {
        "status": "unknown",
        "evidence": "unverified",
        "reason_code": "routing_unverified",
        "client_status": "pass",
    }


def test_rc_report_requires_runtime_evidence_when_stable_lookup_is_offline(tmp_path, monkeypatch):
    module = _script_module()

    def request(_method, url, _api_key, payload=None, **_kwargs):
        if url.endswith("/harnesses"):
            return {
                "harnesses": [{
                    "harness_id": "hermes",
                    "kind": "hermes",
                    "setup": {"content": "model: {}\n"},
                    "connections": [{"name": "default"}],
                }]
            }
        if payload and payload.get("tools"):
            return {"choices": [{"message": {"tool_calls": [{"function": {"name": "imperium_ping"}}]}}]}
        return {"choices": [{"message": {"content": "private model response"}}]}

    monkeypatch.setattr(module, "_request", request)
    monkeypatch.setattr(module, "_probe_installed", lambda _kind: {"status": "pass", "evidence": "observed", "version": "0.21.0"})
    monkeypatch.setattr(module, "_probe_stable", lambda _kind: module._unknown("stable_lookup_failed"))
    monkeypatch.setattr(module, "_run_hermes_canary", lambda *_args: {"status": "pass", "evidence": "tested"})
    output = tmp_path / "evidence.json"

    report = module.smoke(
        "http://private-router:9000",
        "router-secret",
        check_tools=True,
        harness_ids={"hermes"},
        json_output=output,
        collect_evidence=True,
        client_canary_ids={"hermes"},
        require_complete_evidence=True,
    )

    assert report["schema_version"] == 2
    assert report["endpoint_ok"] is True
    assert report["evidence"]["hermes"]["overall"] == "pass"
    assert report["evidence"]["hermes"]["stable"]["status"] == "unknown"
    assert report["evidence"]["hermes"]["stable"]["alignment"] == "unknown"
    serialized = output.read_text(encoding="utf-8")
    assert "router-secret" not in serialized
    assert "private-router" not in serialized
    assert "private model response" not in serialized
    assert module._HERMES_CANARY_PROMPT not in serialized


def test_rc_report_fails_closed_when_client_evidence_is_unknown(tmp_path, monkeypatch):
    module = _script_module()

    def request(_method, url, _api_key, payload=None, **_kwargs):
        if url.endswith("/harnesses"):
            return {"harnesses": [{"harness_id": "pi", "kind": "pi", "connections": [{"name": "default"}]}]}
        if payload and payload.get("tools"):
            return {"choices": [{"message": {"tool_calls": [{"function": {"name": "imperium_ping"}}]}}]}
        return {"choices": [{"message": {"content": "OK"}}]}

    monkeypatch.setattr(module, "_request", request)
    monkeypatch.setattr(module, "_probe_installed", lambda _kind: {"status": "pass", "evidence": "observed", "version": "0.80.6"})
    monkeypatch.setattr(module, "_probe_stable", lambda _kind: {"status": "pass", "evidence": "documented", "version": "0.84.4"})
    output = tmp_path / "unknown.json"

    with pytest.raises(RuntimeError, match="required_harness_evidence_incomplete"):
        module.smoke(
            "http://router:9000",
            check_tools=True,
            harness_ids={"pi"},
            json_output=output,
            collect_evidence=True,
            client_canary_ids={"pi"},
            require_complete_evidence=True,
        )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["endpoint_ok"] is True
    assert report["ok"] is False
    assert report["incomplete_harnesses"] == ["pi"]
    assert report["evidence"]["pi"]["client_canary"]["reason_code"] == "client_canary_not_implemented"
