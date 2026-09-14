"""HTTP behavior while the managed GPU slot changes model."""
from __future__ import annotations

import asyncio
import json
import textwrap

import pytest
import yaml
from starlette.requests import ClientDisconnect
from starlette.testclient import TestClient

from local_model_router.helpers.llama_cpp_manager import BackendManager
from local_model_router.service.app import create_app
from local_model_router.service.fleet_manager import FleetQueue, FleetStore


def _write(path, text):
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


def _app(
    tmp_path,
    monkeypatch,
    *,
    queue=None,
    store=None,
    health=True,
    upstreams_text=None,
):
    BackendManager._instance = None
    monkeypatch.setenv("A0_LMM_ROUTER_ENABLE_FLEET_CONTROL", "1")
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    model = tmp_path / "ornith.gguf"
    model.touch()
    fleet = _write(
        tmp_path / "llama_cpp_servers.yaml",
        f"""
        active_slots:
          - id: slot_chat
            host: localhost
            port: 18888
            role: chat
            enabled: true
            model_id: ornith
            model_path: {model.as_posix()}
            context_size: 131072
            supports_tools: true
            supports_vision: false
            supports_json_mode: true
        global:
          backend: subprocess
          auto_restart: false
        """,
    )
    (tmp_path / "hermes_gpu_profiles.yaml").write_text(
        yaml.safe_dump({
            "slot_id": "slot_chat",
            "hermes_home": str(tmp_path / "hermes"),
            "profiles": {
                "ornith": {
                    "model_id": "ornith",
                    "model_path": str(model),
                    "context_size": 131072,
                    "default_max_tokens": 16384,
                    "supports_vision": False,
                    "extra_args": [],
                }
            },
        }),
        encoding="utf-8",
    )
    harnesses = _write(
        tmp_path / "harnesses.yaml",
        """
        harnesses:
          hermes:
            display_name: Hermes
            kind: hermes
            protocol: openai
            location: host
            connections:
              default: {model: ornith}
        """,
    )

    async def health_probe(_url, _timeout):
        return {"ok": health}

    monkeypatch.setattr(
        "local_model_router.helpers.smart_router.health._aiohttp_probe", health_probe
    )
    app = create_app(
        fleet,
        upstreams_path=(
            _write(tmp_path / "upstreams.yaml", upstreams_text)
            if upstreams_text is not None
            else str(tmp_path / "missing-upstreams.yaml")
        ),
        harnesses_path=harnesses,
        setup_home=str(tmp_path / "imperium"),
        fleet_queue=queue,
        fleet_store=store,
    )
    watcher = app.state.hermes_watcher

    async def no_refresh(**_kwargs):
        return "idle"

    watcher.refresh_desired = no_refresh
    watcher.desired = "ornith"
    watcher.loaded = "ornith"
    return app, BackendManager._instance, watcher


def _patch_forward(monkeypatch, *, finish_reason="stop"):
    import aiohttp

    calls = []

    class Response:
        status = 200

        async def json(self, content_type=None):
            return {
                "id": "chatcmpl-swap",
                "choices": [{"message": {"content": "ok"}, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

        async def text(self):
            return "ok"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        def __init__(self, *_args, **_kwargs):
            pass

        def post(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    return calls


def _chat(client, *, path="/v1/chat/completions", **extra):
    return client.post(
        path,
        json={"model": "ornith", "messages": [{"role": "user", "content": "hello"}], **extra},
    )


async def _asgi_post_json(app, path, payload):
    body = json.dumps(payload).encode()
    delivered = False
    messages = []

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 10000),
            "server": ("127.0.0.1", 9000),
            "root_path": "",
        },
        receive,
        send,
    )
    start = next(message for message in messages if message["type"] == "http.response.start")
    raw = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    headers = {key.decode().lower(): value.decode() for key, value in start["headers"]}
    return start["status"], json.loads(raw or b"{}"), headers


def test_hermes_request_gets_openai_503_and_retry_after_during_swap(tmp_path, monkeypatch):
    store = FleetStore(":memory:")
    updates: list[tuple[str, str, str | None]] = []
    original_update = store.update_request

    def record_update(request_id, *, status, error_code=None, **kwargs):
        updates.append((request_id, status, error_code))
        return original_update(
            request_id, status=status, error_code=error_code, **kwargs
        )

    store.update_request = record_update  # type: ignore[method-assign]
    app, _manager, watcher = _app(tmp_path, monkeypatch, store=store)
    calls = _patch_forward(monkeypatch)
    watcher.retry_response = lambda: ("gpu_swap_in_progress", 17)

    response = _chat(TestClient(app), path="/harnesses/hermes/v1/chat/completions")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "17"
    assert response.json()["error"]["code"] == "gpu_swap_in_progress"
    assert response.json()["hermes_gpu"]["desired"] == "ornith"
    request_id = response.headers["x-a0-request-id"]
    assert updates == [(request_id, "rejected", "gpu_swap_in_progress")]
    assert calls == []


def test_failed_hermes_target_is_not_silently_served_by_restored_model(
    tmp_path, monkeypatch
):
    app, _manager, watcher = _app(tmp_path, monkeypatch)
    calls = _patch_forward(monkeypatch)
    watcher.desired = "qwen38_27b"
    watcher.loaded = "ornith"
    watcher.last_working = "ornith"
    watcher.phase = "failed"
    watcher.failure_code = "process_exited"
    watcher._blocked_target = "qwen38_27b"

    response = _chat(TestClient(app), path="/harnesses/hermes/v1/chat/completions")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "gpu_swap_failed"
    assert response.json()["hermes_gpu"]["loaded"] == "ornith"
    assert "retry-after" not in response.headers
    assert calls == []


def test_manual_stop_blocks_hermes_even_when_desired_matches_loaded(
    tmp_path, monkeypatch
):
    app, manager, watcher = _app(tmp_path, monkeypatch)
    calls = _patch_forward(monkeypatch)
    manager.set_manual_stop("slot_chat", True)
    assert watcher.desired == watcher.loaded == "ornith"
    watcher.failure_code = "process_exited"
    watcher.error = "old failure"

    assert asyncio.run(
        type(watcher).refresh_desired(watcher, probe_loaded=False)
    ) == "stopped"
    assert watcher.failure_code is None
    assert watcher.error is None

    response = _chat(TestClient(app), path="/harnesses/hermes/v1/chat/completions")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "gpu_slot_stopped"
    assert "is stopped" in response.json()["error"]["message"]
    assert "retry-after" not in response.headers
    assert calls == []


def test_backoff_retry_after_comes_from_watchers_next_attempt_at(
    tmp_path, monkeypatch
):
    app, _manager, watcher = _app(tmp_path, monkeypatch)
    calls = _patch_forward(monkeypatch)
    watcher.desired = "qwen38_27b"
    watcher.loaded = "ornith"
    watcher.phase = "backoff"
    watcher.next_attempt_at = 1017.0
    monkeypatch.setattr(
        "local_model_router.service.hermes_model_watcher.time.time", lambda: 1000.0
    )

    response = _chat(TestClient(app), path="/harnesses/hermes/v1/chat/completions")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "17"
    assert response.json()["error"]["code"] == "gpu_swap_in_progress"
    assert response.json()["hermes_gpu"]["retry_after_seconds"] == 17
    assert calls == []


def test_local_request_is_rejected_before_unavailable_routing_during_operation(tmp_path, monkeypatch):
    app, manager, _watcher = _app(tmp_path, monkeypatch, health=False)
    calls = _patch_forward(monkeypatch)
    assert manager.begin_slot_operation("slot_chat", "loading") is True

    async def unexpected_probe(_url, _timeout):
        pytest.fail("slot health must not run after the intended slot closes ingress")

    monkeypatch.setattr(
        "local_model_router.helpers.smart_router.health._aiohttp_probe",
        unexpected_probe,
    )

    response = _chat(TestClient(app))

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["error"]["code"] == "gpu_swap_in_progress"
    assert calls == []
    manager.finish_slot_operation("slot_chat")


def test_request_waiting_in_queue_never_posts_after_slot_epoch_changes(tmp_path, monkeypatch):
    queue = FleetQueue(max_active=1, max_queue=1)
    app, manager, _watcher = _app(tmp_path, monkeypatch, queue=queue)
    calls = _patch_forward(monkeypatch)

    async def scenario():
        await queue.acquire("normal")
        request = asyncio.create_task(
            _asgi_post_json(
                app,
                "/v1/chat/completions",
                {
                    "model": "ornith",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
        )
        for _ in range(100):
            if queue.snapshot()["queued"] == 1:
                break
            await asyncio.sleep(0.001)
        assert queue.snapshot()["queued"] == 1
        assert manager.begin_slot_operation("slot_chat", "loading") is True
        manager.finish_slot_operation("slot_chat")
        await queue.release()
        return await request

    status, payload, headers = asyncio.run(scenario())

    assert status == 503
    assert headers["retry-after"] == "1"
    assert payload["error"]["code"] == "gpu_model_changed"
    assert calls == []
    assert queue.snapshot()["active"] == 0


def test_hermes_request_rechecks_selection_after_waiting_in_queue(
    tmp_path, monkeypatch
):
    queue = FleetQueue(max_active=1, max_queue=1)
    app, _manager, watcher = _app(tmp_path, monkeypatch, queue=queue)
    calls = _patch_forward(monkeypatch)
    refreshes = 0

    async def refresh(**_kwargs):
        nonlocal refreshes
        refreshes += 1
        if refreshes == 2:
            watcher.desired = "qwen38_27b"
        return "idle"

    watcher.refresh_desired = refresh

    async def scenario():
        await queue.acquire("normal")
        request = asyncio.create_task(
            _asgi_post_json(
                app,
                "/harnesses/hermes/v1/chat/completions",
                {
                    "model": "ornith",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
        )
        for _ in range(100):
            if queue.snapshot()["queued"] == 1:
                break
            await asyncio.sleep(0.001)
        assert queue.snapshot()["queued"] == 1
        await queue.release()
        return await request

    status, payload, headers = asyncio.run(scenario())

    assert refreshes == 2
    assert status == 503
    assert headers["retry-after"] == "5"
    assert payload["error"]["code"] == "gpu_swap_in_progress"
    assert calls == []
    assert queue.snapshot()["active"] == 0


def test_explicit_upstream_is_not_blocked_by_local_gpu_swap(tmp_path, monkeypatch):
    upstreams = """
        upstreams:
          - name: cloud
            type: openai_compatible
            base_url: http://cloud.invalid/v1
            enabled: true
            capabilities: [chat]
            models: [remote-chat]
    """
    app, manager, _watcher = _app(
        tmp_path, monkeypatch, upstreams_text=upstreams
    )
    calls = _patch_forward(monkeypatch)
    assert manager.begin_slot_operation("slot_chat", "loading") is True

    response = TestClient(app).post(
        "/v1/chat/completions",
        json={
            "model": "cloud/remote-chat",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["args"][0] == "http://cloud.invalid/v1/chat/completions"
    manager.finish_slot_operation("slot_chat")


def test_unrelated_no_slot_is_not_reported_as_gpu_swap(tmp_path, monkeypatch):
    app, manager, _watcher = _app(tmp_path, monkeypatch, health=False)
    calls = _patch_forward(monkeypatch)
    assert manager.begin_slot_operation("slot_chat", "loading") is True

    response = TestClient(app).post(
        "/v1/chat/completions",
        json={
            "model": "auto",
            "role": "code",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_slot_available"
    assert "retry-after" not in response.headers
    assert calls == []
    manager.finish_slot_operation("slot_chat")


def test_ornith_default_output_limit_applies_only_when_client_omits_it(tmp_path, monkeypatch):
    app, _manager, watcher = _app(tmp_path, monkeypatch)
    watcher.retry_response = lambda: None
    calls = _patch_forward(monkeypatch)
    client = TestClient(app)

    assert _chat(client, path="/harnesses/hermes/v1/chat/completions").status_code == 200
    assert _chat(
        client,
        path="/harnesses/hermes/v1/chat/completions",
        max_tokens=123,
    ).status_code == 200
    assert _chat(
        client,
        path="/harnesses/hermes/v1/chat/completions",
        max_completion_tokens=456,
    ).status_code == 200
    assert _chat(
        client,
        path="/harnesses/hermes/v1/chat/completions",
        max_tokens=None,
    ).status_code == 200
    assert _chat(
        client,
        path="/harnesses/hermes/v1/chat/completions",
        max_completion_tokens=None,
    ).status_code == 200
    assert calls[0]["kwargs"]["json"]["max_tokens"] == 16384
    assert calls[1]["kwargs"]["json"]["max_tokens"] == 123
    assert "max_tokens" not in calls[2]["kwargs"]["json"]
    assert calls[2]["kwargs"]["json"]["max_completion_tokens"] == 456
    assert calls[3]["kwargs"]["json"]["max_tokens"] == 16384
    assert "max_completion_tokens" not in calls[3]["kwargs"]["json"]
    assert calls[4]["kwargs"]["json"]["max_tokens"] == 16384
    assert "max_completion_tokens" not in calls[4]["kwargs"]["json"]


def test_length_finish_reason_is_preserved_and_signaled(tmp_path, monkeypatch):
    app, _manager, watcher = _app(tmp_path, monkeypatch)
    watcher.retry_response = lambda: None
    _patch_forward(monkeypatch, finish_reason="length")

    response = _chat(TestClient(app), path="/harnesses/hermes/v1/chat/completions")

    assert response.status_code == 200
    assert response.json()["choices"][0]["finish_reason"] == "length"
    assert response.headers["x-a0-router-output-truncated"] == "true"


@pytest.mark.parametrize(
    ("effective_n_ctx", "header_value"),
    [(65536, "65536"), (None, "unknown")],
)
def test_measured_context_reaches_models_status_and_chat_headers(
    tmp_path, monkeypatch, effective_n_ctx, header_value
):
    app, _manager, watcher = _app(tmp_path, monkeypatch)
    watcher.retry_response = lambda: None
    watcher.effective_n_ctx = effective_n_ctx
    watcher.configured_n_ctx = 131072
    watcher.context_source = "props" if effective_n_ctx else None
    app.state.observer.set_runtime_slot(
        "slot_chat",
        model_id="ornith",
        configured_n_ctx=131072,
        effective_n_ctx=effective_n_ctx,
        context_source=watcher.context_source,
        supports_tools=True,
        supports_vision=False,
        supports_json_mode=True,
    )
    _patch_forward(monkeypatch)
    client = TestClient(app)

    models = {row["id"]: row for row in client.get("/v1/models").json()["data"]}
    status = client.get("/ui/status").json()["hermes_gpu"]
    response = _chat(client, path="/harnesses/hermes/v1/chat/completions")

    assert models["ornith"]["context_size"] == effective_n_ctx
    assert models["ornith"]["meta"]["configured_n_ctx"] == 131072
    assert models["ornith"]["meta"]["effective_n_ctx"] == effective_n_ctx
    assert status["effective_n_ctx"] == effective_n_ctx
    assert response.headers["x-hard-ctx"] == header_value


def test_stream_keeps_slot_lease_until_done(tmp_path, monkeypatch):
    import aiohttp

    app, manager, watcher = _app(tmp_path, monkeypatch)
    watcher.retry_response = lambda: None
    active_during_stream: list[int] = []

    class Content:
        async def iter_chunked(self, _size):
            active_during_stream.append(
                manager.slot_operation_snapshot("slot_chat")["active_requests"]
            )
            yield b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

    class Response:
        status = 200
        content = Content()

        def release(self):
            pass

    class Session:
        def __init__(self, *_args, **_kwargs):
            pass

        async def post(self, *_args, **_kwargs):
            return Response()

        async def close(self):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", Session)

    response = _chat(
        TestClient(app),
        path="/harnesses/hermes/v1/chat/completions",
        stream=True,
    )

    assert response.status_code == 200
    assert active_during_stream == [1]
    assert manager.slot_operation_snapshot("slot_chat")["active_requests"] == 0


def test_stream_send_disconnect_closes_slot_lease_queue_and_upstream(
    tmp_path, monkeypatch
):
    import aiohttp

    queue = FleetQueue(max_active=1, max_queue=1)
    app, manager, watcher = _app(tmp_path, monkeypatch, queue=queue)
    watcher.retry_response = lambda: None
    state = {"released": False, "closed": False}

    class Content:
        async def iter_chunked(self, _size):
            yield b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
            await asyncio.Event().wait()

    class Response:
        status = 200
        content = Content()

        def release(self):
            state["released"] = True

    class Session:
        def __init__(self, *_args, **_kwargs):
            pass

        async def post(self, *_args, **_kwargs):
            return Response()

        async def close(self):
            state["closed"] = True

    monkeypatch.setattr(aiohttp, "ClientSession", Session)

    async def scenario():
        body = json.dumps(
            {
                "model": "ornith",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            }
        ).encode()
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                raise OSError("client disconnected")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/harnesses/hermes/v1/chat/completions",
            "raw_path": b"/harnesses/hermes/v1/chat/completions",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 10000),
            "server": ("127.0.0.1", 9000),
            "root_path": "",
        }
        with pytest.raises((OSError, ClientDisconnect)):
            await app(scope, receive, send)

    asyncio.run(scenario())
    assert state == {"released": True, "closed": True}
    assert manager.slot_operation_snapshot("slot_chat")["active_requests"] == 0
    assert queue.snapshot()["active"] == 0


def test_request_preparation_failure_closes_slot_lease_and_queue(tmp_path, monkeypatch):
    import local_model_router.service.app as app_module

    queue = FleetQueue(max_active=1, max_queue=1)
    app, manager, watcher = _app(tmp_path, monkeypatch, queue=queue)
    watcher.retry_response = lambda: None

    monkeypatch.setattr(
        app_module,
        "_context_for_slot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected")),
    )

    response = _chat(TestClient(app), path="/harnesses/hermes/v1/chat/completions")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "request_preparation_failed"
    assert manager.slot_operation_snapshot("slot_chat")["active_requests"] == 0
    assert queue.snapshot()["active"] == 0


def test_slot_lease_failure_closes_admission_queue(tmp_path, monkeypatch):
    queue = FleetQueue(max_active=1, max_queue=1)
    app, manager, watcher = _app(tmp_path, monkeypatch, queue=queue)
    watcher.retry_response = lambda: None
    monkeypatch.setattr(
        manager,
        "try_begin_slot_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected")),
    )

    response = _chat(TestClient(app), path="/harnesses/hermes/v1/chat/completions")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "request_preparation_failed"
    assert manager.slot_operation_snapshot("slot_chat")["active_requests"] == 0
    assert queue.snapshot()["active"] == 0


def test_cancelled_request_is_removed_from_queue_and_finished_once(tmp_path, monkeypatch):
    queue = FleetQueue(max_active=1, max_queue=1)
    store = FleetStore(":memory:")
    terminal_updates: list[tuple[str, str | None]] = []
    original_update = store.update_request

    def track_update(request_id, *, status, error_code=None, **kwargs):
        if status in {"completed", "failed", "rejected"}:
            terminal_updates.append((status, error_code))
        return original_update(request_id, status=status, error_code=error_code, **kwargs)

    store.update_request = track_update  # type: ignore[method-assign]
    app, _manager, _watcher = _app(
        tmp_path, monkeypatch, queue=queue, store=store
    )

    async def scenario():
        await queue.acquire("normal")
        request = asyncio.create_task(
            _asgi_post_json(
                app,
                "/v1/chat/completions",
                {
                    "model": "ornith",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
        )
        for _ in range(100):
            if queue.snapshot()["queued"] == 1:
                break
            await asyncio.sleep(0.001)
        assert queue.snapshot() == {
            "active": 1,
            "queued": 1,
            "max_active": 1,
            "max_queue": 1,
        }
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert queue.snapshot()["queued"] == 0
        await queue.release()

    asyncio.run(scenario())

    assert queue.snapshot()["active"] == 0
    assert terminal_updates == [("failed", "client_disconnected")]
    assert store.request_summary()["by_status"] == {"failed": 1}


def test_admission_telemetry_failure_releases_queue_and_finishes_once(
    tmp_path, monkeypatch
):
    queue = FleetQueue(max_active=1, max_queue=1)
    store = FleetStore(":memory:")
    terminal_updates: list[tuple[str, str | None]] = []
    original_update = store.update_request

    def track_update(request_id, *, status, error_code=None, **kwargs):
        if status in {"completed", "failed", "rejected"}:
            terminal_updates.append((status, error_code))
        return original_update(request_id, status=status, error_code=error_code, **kwargs)

    def fail_admitted_event(**kwargs):
        if kwargs["event_type"] == "admitted":
            raise RuntimeError("injected")

    store.update_request = track_update  # type: ignore[method-assign]
    store.record_queue_event = fail_admitted_event  # type: ignore[method-assign]
    app, _manager, _watcher = _app(
        tmp_path, monkeypatch, queue=queue, store=store
    )
    calls = _patch_forward(monkeypatch)

    response = _chat(TestClient(app))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "request_admission_failed"
    assert calls == []
    assert queue.snapshot()["active"] == 0
    assert queue.snapshot()["queued"] == 0
    assert terminal_updates == [("failed", "request_admission_failed")]
    assert store.request_summary()["by_status"] == {"failed": 1}


def test_persistent_request_telemetry_failure_still_releases_queue(
    tmp_path, monkeypatch
):
    queue = FleetQueue(max_active=1, max_queue=1)
    store = FleetStore(":memory:")
    attempted_statuses: list[str] = []

    def fail_update(_request_id, *, status, **_kwargs):
        attempted_statuses.append(status)
        raise RuntimeError("injected")

    store.update_request = fail_update  # type: ignore[method-assign]
    app, _manager, _watcher = _app(
        tmp_path, monkeypatch, queue=queue, store=store
    )
    calls = _patch_forward(monkeypatch)

    response = _chat(TestClient(app))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "request_admission_failed"
    assert calls == []
    assert queue.snapshot()["active"] == 0
    assert queue.snapshot()["queued"] == 0
    assert attempted_statuses == ["admitted", "failed"]


def test_prepare_shutdown_stops_watcher_then_managed_slots(tmp_path, monkeypatch):
    app, manager, watcher = _app(tmp_path, monkeypatch)
    events: list[str] = []

    async def stop_watcher():
        events.append("watcher")

    async def stop_all():
        events.append("slots")
        return {"slot_chat": True}

    watcher.stop = stop_watcher
    manager.stop_all = stop_all

    response = TestClient(app).post("/fleet/prepare-shutdown", json={})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert events == ["watcher", "slots"]


def test_prepare_shutdown_succeeds_when_fleet_control_is_disabled(tmp_path, monkeypatch):
    BackendManager._instance = None
    monkeypatch.delenv("A0_LMM_ROUTER_ENABLE_FLEET_CONTROL", raising=False)
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    fleet = _write(
        tmp_path / "llama_cpp_servers.yaml",
        """
        active_slots: []
        global:
          backend: remote
        """,
    )
    app = create_app(
        fleet,
        harnesses_path=str(tmp_path / "missing-harnesses.yaml"),
        setup_home=str(tmp_path / "imperium"),
    )

    response = TestClient(app).post("/fleet/prepare-shutdown", json={})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "action": "prepare_shutdown", "results": {}}
