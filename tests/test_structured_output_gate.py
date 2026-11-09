"""The structured-output gate on the chat forwarding paths.

Default posture is observe: the verdict rides in a header and the body is
returned exactly as the model sent it. A0_LMM_ROUTER_STRUCTURED_OUTPUT=1
repairs once and then refuses with 422.

Hermetic: aiohttp is stubbed, no fleet and no network.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_model_router import structured_output as so  # noqa: E402
from local_model_router.service.app import create_app  # noqa: E402

_PROSE = "Sure! Here is the answer you wanted."

_FLEET = textwrap.dedent("""
    active_slots:
      - id: chat
        host: localhost
        port: 8080
        role: chat
        enabled: true
        model_id: test-chat
    global:
      backend: remote
""")

_UPSTREAMS = textwrap.dedent("""
    upstreams:
      - name: up
        type: openai_compatible
        base_url: http://localhost:11434/v1
        enabled: true
        capabilities: [chat, models, json_mode]
""")


def _write(tmp_path, name, content):
    target = tmp_path / name
    target.write_text(content, encoding="utf-8")
    return str(target)


def _reply(content):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }


@pytest.fixture()
def forwards(monkeypatch):
    """Stub aiohttp. `forwards.bodies` is the reply queue, one per POST."""
    import aiohttp

    calls: list[dict] = []
    queue: list[dict] = []

    class FakeResponse:
        status = 200

        def __init__(self):
            self._payload = queue.pop(0) if queue else _reply('{"ok": true}')

        async def json(self, content_type=None):
            return self._payload

        async def text(self):
            return "not-json"

        def release(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    class FakeSession:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        def post(self, *args, **kwargs):
            calls.append({"url": args[0] if args else kwargs.get("url"), "json": kwargs.get("json")})
            return FakeResponse()

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)

    class Handle:
        def __init__(self):
            self.calls = calls

        def queue(self, *payloads):
            queue.extend(payloads)

    return Handle()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    monkeypatch.delenv(so.ENFORCE_ENV, raising=False)
    monkeypatch.delenv("A0_LMM_ROUTER_PROMPT_CACHE", raising=False)

    async def healthy(url, timeout):
        return {"ok": True}

    monkeypatch.setattr(
        "local_model_router.helpers.smart_router.health._aiohttp_probe", healthy
    )
    from local_model_router.helpers.llama_cpp_manager import BackendManager

    BackendManager._instance = None
    return TestClient(
        create_app(
            _write(tmp_path, "llama_cpp_servers.yaml", _FLEET),
            upstreams_path=_write(tmp_path, "upstreams.yaml", _UPSTREAMS),
            apps_path=str(tmp_path / "missing-apps.yaml"),
        )
    )


def _ask(client, *, model="auto", json_mode=True, stream=False):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if stream:
        body["stream"] = True
    return client.post("/v1/chat/completions", json=body)


HEADER = "x-a0-router-structured-output"


# ---------------------------------------------------------------------------
# Observe by default
# ---------------------------------------------------------------------------

class TestObserveByDefault:
    def test_valid_json_is_marked_valid(self, client, forwards):
        forwards.queue(_reply('{"answer": 42}'))
        resp = _ask(client)
        assert resp.status_code == 200
        assert resp.headers[HEADER] == so.VALID

    def test_prose_is_marked_failed_but_still_returned(self, client, forwards):
        forwards.queue(_reply(_PROSE))
        resp = _ask(client)
        assert resp.status_code == 200
        assert resp.headers[HEADER] == so.FAILED
        assert resp.json()["choices"][0]["message"]["content"] == _PROSE

    def test_no_repair_is_attempted_while_observing(self, client, forwards):
        forwards.queue(_reply(_PROSE))
        _ask(client)
        assert len(forwards.calls) == 1

    def test_a_request_without_response_format_is_untouched(self, client, forwards):
        forwards.queue(_reply(_PROSE))
        resp = _ask(client, json_mode=False)
        assert resp.status_code == 200
        assert HEADER not in resp.headers

    def test_a_fenced_reply_counts_as_valid(self, client, forwards):
        forwards.queue(_reply('```json\n{"answer": 42}\n```'))
        assert _ask(client).headers[HEADER] == so.VALID


# ---------------------------------------------------------------------------
# Opt-in enforcement
# ---------------------------------------------------------------------------

class TestEnforcement:
    @pytest.fixture(autouse=True)
    def _enforce(self, monkeypatch, client):
        # Depends on `client` so it runs after it: the client fixture clears
        # this variable, and an autouse fixture would otherwise run first.
        monkeypatch.setenv(so.ENFORCE_ENV, "1")

    def test_a_bad_reply_is_repaired_once(self, client, forwards):
        forwards.queue(_reply(_PROSE), _reply('{"answer": 42}'))
        resp = _ask(client)
        assert resp.status_code == 200
        assert resp.headers[HEADER] == so.REPAIRED
        assert resp.json()["choices"][0]["message"]["content"] == '{"answer": 42}'
        assert len(forwards.calls) == 2

    def test_the_repair_carries_an_extra_instruction_to_the_same_url(self, client, forwards):
        forwards.queue(_reply(_PROSE), _reply("{}"))
        _ask(client)
        first, second = forwards.calls
        assert first["url"] == second["url"]
        assert len(second["json"]["messages"]) == len(first["json"]["messages"]) + 1
        assert second["json"]["messages"][-1]["role"] == "system"

    def test_two_failures_return_422_and_do_not_retry_again(self, client, forwards):
        forwards.queue(_reply(_PROSE), _reply("still not json"))
        resp = _ask(client)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == so.FAILED
        assert len(forwards.calls) == 2

    def test_valid_json_is_unaffected_by_enforcement(self, client, forwards):
        forwards.queue(_reply('{"answer": 42}'))
        resp = _ask(client)
        assert resp.status_code == 200
        assert len(forwards.calls) == 1

    def test_the_flag_is_read_per_request(self, client, forwards, monkeypatch):
        forwards.queue(_reply(_PROSE), _reply(_PROSE))
        assert _ask(client).status_code == 422
        monkeypatch.setenv(so.ENFORCE_ENV, "0")
        forwards.queue(_reply(_PROSE))
        assert _ask(client).status_code == 200


# ---------------------------------------------------------------------------
# A failed reply must never be cached
# ---------------------------------------------------------------------------

class TestCaching:
    def test_a_failed_reply_is_not_served_again_from_the_cache(
        self, tmp_path, monkeypatch, forwards
    ):
        """Caching a prose reply would serve it again as a hit, skipping the
        check entirely — the failure would become permanent and invisible."""
        monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
        monkeypatch.delenv(so.ENFORCE_ENV, raising=False)
        monkeypatch.setenv("A0_LMM_ROUTER_PROMPT_CACHE", "1")

        async def healthy(url, timeout):
            return {"ok": True}

        monkeypatch.setattr(
            "local_model_router.helpers.smart_router.health._aiohttp_probe", healthy
        )
        from local_model_router.helpers.llama_cpp_manager import BackendManager

        BackendManager._instance = None
        app = create_app(
            _write(tmp_path, "llama_cpp_servers.yaml", _FLEET),
            upstreams_path=_write(tmp_path, "upstreams.yaml", _UPSTREAMS),
            apps_path=str(tmp_path / "missing-apps.yaml"),
        )
        http = TestClient(app)

        body = {
            "model": "auto",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        forwards.queue(_reply(_PROSE), _reply('{"answer": 42}'))

        first = http.post("/v1/chat/completions", json=body)
        assert first.headers[HEADER] == so.FAILED
        assert first.headers["x-a0-router-cache"] in {"MISS", "BYPASS"}

        second = http.post("/v1/chat/completions", json=body)
        assert second.headers.get("x-a0-router-cache") != "HIT"
        assert len(forwards.calls) == 2


# ---------------------------------------------------------------------------
# Streaming cannot be checked, and says so
# ---------------------------------------------------------------------------

class TestStreaming:
    def test_a_streamed_json_request_is_reported_as_unchecked(
        self, tmp_path, monkeypatch
    ):
        import aiohttp

        monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)

        async def healthy(url, timeout):
            return {"ok": True}

        monkeypatch.setattr(
            "local_model_router.helpers.smart_router.health._aiohttp_probe", healthy
        )

        class FakeContent:
            async def iter_chunked(self, _size):
                yield b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"stop"}]}\n\n'
                yield b"data: [DONE]\n\n"

        class FakeResponse:
            status = 200
            content = FakeContent()

            def release(self):
                return None

        class FakeSession:
            def __init__(self, *_a, **_kw):
                pass

            async def post(self, *_a, **_kw):
                return FakeResponse()

            async def close(self):
                return None

        monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: FakeSession())
        from local_model_router.helpers.llama_cpp_manager import BackendManager

        BackendManager._instance = None
        http = TestClient(
            create_app(
                _write(tmp_path, "llama_cpp_servers.yaml", _FLEET),
                upstreams_path=_write(tmp_path, "upstreams.yaml", _UPSTREAMS),
                apps_path=str(tmp_path / "missing-apps.yaml"),
            )
        )
        with http.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {"type": "json_object"},
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers[HEADER] == so.UNSUPPORTED


# ---------------------------------------------------------------------------
# Nothing the model said may reach a header or an error body (G4)
# ---------------------------------------------------------------------------

class TestNoLeak:
    def test_observe_mode_leaks_nothing_into_headers(self, client, forwards):
        forwards.queue(_reply(_PROSE))
        resp = _ask(client)
        assert _PROSE not in str(dict(resp.headers))

    def test_the_422_body_names_no_model_output(self, client, forwards, monkeypatch):
        monkeypatch.setenv(so.ENFORCE_ENV, "1")
        forwards.queue(_reply(_PROSE), _reply(_PROSE))
        resp = _ask(client)
        assert resp.status_code == 422
        assert _PROSE not in resp.text
        assert "Sure" not in resp.text
