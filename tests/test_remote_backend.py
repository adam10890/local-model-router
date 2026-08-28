"""Hermetic HTTP contract tests for the externally managed backend."""
from __future__ import annotations

import asyncio
import logging

import aiohttp
import pytest

from local_model_router.helpers.backends.base import BackendType
from local_model_router.helpers.backends.remote_backend import RemoteBackend


def _run(awaitable):
    return asyncio.run(awaitable)


def _fake_http(monkeypatch):
    state = {
        "status": 200,
        "payload": {"status": "ok"},
        "json_error": None,
        "request_error": None,
    }
    calls = []

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        @property
        def status(self):
            return state["status"]

        async def json(self):
            if state["json_error"]:
                raise state["json_error"]
            return state["payload"]

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, url, *, timeout):
            calls.append((url, timeout.total))
            if state["request_error"]:
                raise state["request_error"]
            return Response()

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    return state, calls


def _backend():
    return RemoteBackend(
        {"lmm_hosts": {"chat": "router.internal:9010"}, "startup_timeout": 1}
    )


def test_start_health_list_and_stop_are_direct_registration(monkeypatch):
    _state, calls = _fake_http(monkeypatch)
    backend = _backend()

    started = _run(
        backend.start_slot("chat-slot", {"role": "chat", "model_id": "local-chat"})
    )
    listed = _run(backend.list_slots())

    assert backend.backend_type is BackendType.REMOTE
    assert started.running is True and started.healthy is True
    assert listed == {"chat-slot": started}
    assert calls == [("http://router.internal:9010/health", 5)]
    assert backend.get_endpoint("chat-slot") == "http://router.internal:9010/v1"
    assert backend.get_endpoint_by_role("chat") == "http://router.internal:9010/v1"

    assert _run(backend.stop_slot("chat-slot")) is True
    assert started.running is False and started.healthy is False
    assert _run(backend.stop_slot("missing")) is False


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("timeout", "Remote health check timed out"),
        ("unavailable", "Remote server unavailable"),
        ("malformed", "Remote health response was malformed"),
        ("invalid_json", "Remote health response was malformed"),
        ("http_error", "Remote health check failed"),
    ],
)
def test_health_failures_are_explicit_and_sanitized(monkeypatch, failure, expected):
    state, _calls = _fake_http(monkeypatch)
    backend = _backend()
    _run(backend.start_slot("chat-slot", {"role": "chat"}))

    if failure == "timeout":
        state["request_error"] = asyncio.TimeoutError("secret timeout detail")
    elif failure == "unavailable":
        state["request_error"] = aiohttp.ClientConnectionError(
            "secret-host.example refused"
        )
    elif failure == "malformed":
        state["payload"] = ["not", "an", "object"]
    elif failure == "invalid_json":
        state["json_error"] = ValueError("secret malformed body")
    else:
        state["status"] = 503

    status = _run(backend.health_check("chat-slot"))

    assert status.running is False and status.healthy is False
    assert status.error == expected
    assert "secret" not in status.error


def test_successful_health_recovery_clears_stale_error(monkeypatch):
    state, _calls = _fake_http(monkeypatch)
    backend = _backend()
    _run(backend.start_slot("chat-slot", {"role": "chat"}))

    state["payload"] = []
    failed = _run(backend.health_check("chat-slot"))
    assert failed.error == "Remote health response was malformed"

    state["payload"] = {"status": "ok"}
    recovered = _run(backend.health_check("chat-slot"))
    assert recovered.running is True and recovered.healthy is True
    assert recovered.error is None


def test_invalid_host_is_registered_as_a_sanitized_failure(caplog):
    caplog.set_level(logging.INFO)
    backend = RemoteBackend(
        {"lmm_hosts": {"chat": "https://user:top-secret@example.test/health"}}
    )

    status = _run(backend.start_slot("chat-slot", {"role": "chat"}))

    assert status.running is False and status.healthy is False
    assert status.host == "" and status.port == 0
    assert status.error == "Invalid remote host configuration"
    assert _run(backend.list_slots()) == {"chat-slot": status}
    assert "top-secret" not in caplog.text


def test_role_lookup_never_falls_back_to_an_unprobed_host():
    backend = _backend()

    assert backend.backend_type is BackendType.REMOTE
    assert backend.get_endpoint_by_role("chat") is None
    assert backend.get_all_endpoints() == {}


def test_unknown_slot_and_cleanup_do_not_touch_remote_lifecycle(monkeypatch):
    _state, _calls = _fake_http(monkeypatch)
    backend = _backend()

    unknown = _run(backend.health_check("missing"))
    assert unknown.error == "Unknown slot"

    _run(backend.start_slot("chat-slot", {"role": "chat"}))
    _run(backend.cleanup())
    assert _run(backend.list_slots()) == {}
