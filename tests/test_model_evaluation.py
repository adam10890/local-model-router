from __future__ import annotations

import json

import pytest

from local_model_router import evaluation
from local_model_router.evaluation import EVALUATION_SUITE_VERSION, _valid_scribe_fidelity, evaluate_models
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
        elif "command pytest -q" in prompt:
            message = {"content": '{"command":"pytest -q","failed":2,"fix_applied":false}'}
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
    assert first["models"][0]["roles"]["scribe"]["cases"] == 2
    assert first["suite_version"] == EVALUATION_SUITE_VERSION
    assert first_posts == 6
    assert posts == first_posts
    assert second["models"][0]["skipped_reason"] == "unchanged"
    assert "IMPERIUM_OK" not in str(store.latest_model_snapshot("model_evaluation"))
    assert "pytest -q" not in str(store.latest_model_snapshot("model_evaluation"))
    assert "fix_applied" not in str(store.latest_model_snapshot("model_evaluation"))


@pytest.mark.parametrize("facts", [
    {"command": "pytest -q", "failed": 3, "fix_applied": False},
    {"command": "pytest -q", "failed": 2, "fix_applied": True},
    {"command": "pytest -q", "failed": 2, "fix_applied": 0},
    {"command": "pytest -q", "failed": 2.0, "fix_applied": False},
    {"command": "pytest -q", "failed": True, "fix_applied": False},
    {"command": "pytest -q", "failed": "2", "fix_applied": False},
    {"command": "pytest", "failed": 2, "fix_applied": False},
    {"command": "pytest -q", "failed": 2, "fix_applied": False, "passed": 10},
    {"command": "pytest -q", "failed": 2},
    [],
    None,
])
def test_scribe_fidelity_rejects_changed_invented_or_mistyped_facts(facts):
    assert not _valid_scribe_fidelity({"choices": [{"message": {"content": json.dumps(facts)}}]})


@pytest.mark.parametrize("content", ["", "2 failed", "{bad json", "```json\n{}\n```", {}, None])
def test_scribe_fidelity_rejects_malformed_answers(content):
    assert not _valid_scribe_fidelity({"choices": [{"message": {"content": content}}]})


def test_scribe_fidelity_accepts_only_observed_facts():
    content = ' {"fix_applied": false, "failed": 2, "command": "pytest -q"} '
    assert _valid_scribe_fidelity({"choices": [{"message": {"content": content}}]})
    assert not _valid_scribe_fidelity({"choices": []})


def test_evaluator_invalidates_previous_suite_and_force_reruns(monkeypatch):
    store = FleetStore(":memory:")
    posts = 0

    def request(method, path, payload=None):
        nonlocal posts
        if path == "/routing/models":
            return {"models": [{
                "source": "local_fleet", "model_id": "test-model", "slot_id": "scribe", "role": "scribe",
            }]}
        if path == "/health/slots":
            return [{"id": "scribe", "health": "healthy"}]
        if method == "GET":
            return {}
        posts += 1
        return {"choices": [{"message": {"content": ""}}]}

    with monkeypatch.context() as previous_suite:
        previous_suite.setattr(evaluation, "EVALUATION_SUITE_VERSION", EVALUATION_SUITE_VERSION - 1)
        old = evaluate_models(request, store)
    old.pop("suite_version")
    store.record_model_snapshot("model_evaluation", old)
    posts = 0

    refreshed = evaluate_models(request, store)
    assert posts == 6
    assert refreshed["suite_version"] == EVALUATION_SUITE_VERSION
    assert refreshed["models"][0]["fingerprint"] != old["models"][0]["fingerprint"]
    assert refreshed["models"][0]["roles"]["scribe"]["cases"] == 2
    assert refreshed["models"][0]["skipped_reason"] is None

    unchanged = evaluate_models(request, store)
    assert posts == 6
    assert unchanged["models"][0]["skipped_reason"] == "unchanged"

    forced = evaluate_models(request, store, force=True)
    assert posts == 12
    assert forced["models"][0]["skipped_reason"] is None


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
    assert posts == 6


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
