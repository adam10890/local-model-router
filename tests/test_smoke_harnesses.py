from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path

import pytest


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
