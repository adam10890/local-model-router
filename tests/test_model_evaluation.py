from __future__ import annotations

from local_model_router.evaluation import evaluate_models
from local_model_router.service.fleet_manager import FleetStore


def test_evaluator_records_metrics_and_skips_unchanged_models():
    store = FleetStore(":memory:")
    posts = 0
    scans = 0

    def request(method, path, payload=None):
        nonlocal posts, scans
        if path == "/routing/models":
            return {"models": [{
                "source": "local_fleet",
                "model_id": "test-model",
                "slot_id": "chat",
                "role": "chat",
            }]}
        if path == "/ui/status":
            scans += 1
            return {
                "hardware": {"gpu": "test", "ram_mb": 32_000, "ram_available_mb": 20_000 - scans},
                "setup": {"discovery": {"local_models": [], "path_runtime": "/runtime/llama-server"}},
            }
        if path == "/health/slots":
            return [{"id": "chat", "health": "healthy"}]
        if path == "/cookbook":
            return {"models": []}
        if path == "/v1/embeddings":
            posts += 1
            return {"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}
        posts += 1
        prompt = payload["messages"][0]["content"]
        message = {"content": "IMPERIUM_OK"}
        if payload.get("response_format"):
            message = {"content": '{"ok":true}'}
        elif payload.get("tools"):
            message = {"tool_calls": [{"function": {"name": "imperium_probe", "arguments": '{"value":7}'}}]}
        elif "function add" in prompt:
            message = {"content": "def add(a, b):\n    return a + b"}
        elif "project Orion" in prompt:
            message = {"content": "Orion has 17 tasks and uses blue."}
        return {
            "choices": [{"message": message}],
            "usage": {"completion_tokens": 4},
        }

    first = evaluate_models(request, store)
    first_posts = posts
    second = evaluate_models(request, store)

    assert first["models"][0]["roles"]["chat"]["pass_rate"] == 1.0
    assert first["models"][0]["roles"]["utility"]["reliability"] == 1.0
    assert first["models"][0]["roles"]["scribe"]["pass_rate"] == 1.0
    assert first_posts == 5
    assert posts == first_posts
    assert second["models"][0]["skipped_reason"] == "unchanged"
    assert "IMPERIUM_OK" not in str(store.latest_model_snapshot("model_evaluation"))


def test_evaluator_marks_unreachable_model_without_storing_error_text():
    store = FleetStore(":memory:")
    posts = 0

    def request(method, path, payload=None):
        nonlocal posts
        if path == "/routing/models":
            return {"models": [{
                "source": "local_fleet",
                "model_id": "offline-model",
                "slot_id": "chat",
                "role": "chat",
            }]}
        if method == "GET":
            return {}
        posts += 1
        raise RuntimeError("private upstream error body")

    payload = evaluate_models(request, store)

    assert payload["models"][0]["skipped_reason"] == "unreachable"
    assert payload["models"][0]["roles"] == {}
    assert posts == 0
    assert "private upstream error body" not in str(payload)


def test_evaluator_retries_a_model_that_becomes_reachable():
    store = FleetStore(":memory:")
    online = False
    posts = 0

    def request(method, path, payload=None):
        nonlocal posts
        if path == "/routing/models":
            return {"models": [{
                "source": "local_fleet",
                "model_id": "recovering-model",
                "slot_id": "chat",
                "role": "chat",
            }]}
        if path == "/health/slots":
            return [{"id": "chat", "health": "healthy" if online else "unhealthy"}]
        if path in {"/ui/status", "/cookbook"}:
            return {}
        posts += 1
        return {"choices": [{"message": {"content": ""}}]}

    first = evaluate_models(request, store)
    online = True
    second = evaluate_models(request, store)

    assert first["models"][0]["skipped_reason"] == "unreachable"
    assert second["models"][0]["skipped_reason"] is None
    assert second["models"][0]["roles"]["chat"]["reliability"] == 1.0
    assert posts == 5


def test_embedding_evaluation_requires_stable_finite_dimensions():
    store = FleetStore(":memory:")

    def request(method, path, payload=None):
        if path == "/routing/models":
            return {"models": [{
                "source": "local_fleet",
                "model_id": "embed-model",
                "slot_id": "embedding",
                "role": "embed",
            }]}
        if path == "/v1/embeddings":
            assert len(payload["input"]) == 2
            return {"data": [
                {"embedding": [0.1, 0.2]},
                {"embedding": [0.3, 0.4]},
            ]}
        if path == "/health/slots":
            return [{"id": "embedding", "health": "healthy"}]
        return {}

    payload = evaluate_models(request, store)

    assert payload["models"][0]["roles"]["embed"]["pass_rate"] == 1.0
