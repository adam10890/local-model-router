"""Hermetic Studio lease and a virtual Hermes post-tool retry driver."""

import pytest
from starlette.testclient import TestClient

from local_model_router.helpers.backends.base import SlotStatus
from test_gpu_swap_api import _app, _chat, _patch_forward


@pytest.mark.parametrize(
    ("recovery_cycles", "release_at", "expected_attempts", "expected_seconds", "expected_ack"),
    [
        pytest.param(0, None, 3, 240, None, id="primary-attempts-only"),
        pytest.param(5, None, 18, 2040, None, id="default-recovery-exhausted"),
        pytest.param(5, 120, 2, 120, 120, id="lease-released-before-retry"),
    ],
)
def test_synthetic_studio_claim_holds_post_tool_ack(
    tmp_path, monkeypatch, recovery_cycles, release_at, expected_attempts,
    expected_seconds, expected_ack,
):
    app, manager, _watcher = _app(tmp_path, monkeypatch)
    forwarded = _patch_forward(monkeypatch)

    async def fake_stop(_name):
        return True

    async def fake_start(name, _config):
        return SlotStatus(name=name, running=True, healthy=True, port=18888, host="localhost")

    monkeypatch.setattr(manager._backend, "stop_slot", fake_stop)
    monkeypatch.setattr(manager._backend, "start_slot", fake_start)
    client = TestClient(app)

    def submit_fake_studio_job():
        job_id = "synthetic-render"
        lease = client.post("/fleet/slots/slot_chat/stop", json={"reason": "studio_lease"})
        assert lease.status_code == 200, lease.text
        assert manager.slot_operation_snapshot("slot_chat")["lease_reason"] == "studio_lease"
        return job_id

    # The fake Studio accepts the job and claims the GPU before returning the tool result.
    job_id = submit_fake_studio_job()
    messages = [
        {"role": "user", "content": "Start a synthetic render."},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "submit-render",
                "type": "function",
                "function": {"name": "submit_render", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "submit-render", "content": job_id},
    ]
    elapsed = 0
    attempt_times = []
    acknowledgment = None
    # Hermes v0.23.3 (caafe5389a), agent/conversation_loop.py and agent/turn_api_error.py:
    # api_max_retries=3 caps total attempts per cycle. With no fallback and no delivered
    # answer, an enabled auto-recovery cycle waits and resets the attempt counter.
    # Both backoff paths honor this response's Retry-After of 120 seconds.
    budget = 3 * (1 + recovery_cycles)
    for attempt in range(1, budget + 1):
        attempt_times.append(elapsed)
        response = _chat(client, path="/harnesses/hermes/v1/chat/completions", messages=messages)
        if response.status_code == 200:
            assert response.json()["choices"][0]["message"]["content"] == "ok"
            acknowledgment = (job_id, elapsed)
            break
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "gpu_leased_by_studio"
        assert response.headers["retry-after"] == "120"
        if attempt < budget:
            elapsed += int(response.headers["retry-after"])
            if elapsed == release_at:
                released = client.post("/fleet/slots/slot_chat/start")
                assert released.status_code == 200, released.text

    assert attempt_times == list(range(0, expected_attempts * 120, 120))
    assert elapsed == expected_seconds
    assert acknowledgment == (None if expected_ack is None else (job_id, expected_ack))
    assert len(forwarded) == int(expected_ack is not None)
