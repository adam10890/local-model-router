from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def test_harness_smoke_checks_models_and_completion(monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "smoke_harnesses.py"
    assert script.exists()
    spec = spec_from_file_location("smoke_harnesses", script)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def request(method, url, api_key, payload=None, timeout=120):
        calls.append((method, url, payload))
        if url.endswith("/harnesses"):
            return {
                "harnesses": [{
                    "harness_id": "agent_zero",
                    "connections": [{"name": "chat"}, {"name": "utility"}],
                }]
            }
        return {}

    monkeypatch.setattr(module, "_request", request)
    module.smoke("http://router:9000", "secret")

    assert [call[:2] for call in calls] == [
        ("GET", "http://router:9000/harnesses"),
        ("GET", "http://router:9000/harnesses/agent_zero/chat/v1/models"),
        ("POST", "http://router:9000/harnesses/agent_zero/chat/v1/chat/completions"),
        ("GET", "http://router:9000/harnesses/agent_zero/utility/v1/models"),
        ("POST", "http://router:9000/harnesses/agent_zero/utility/v1/chat/completions"),
    ]
