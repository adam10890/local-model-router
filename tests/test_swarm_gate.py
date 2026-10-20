"""Swarm gate: slot atomicity, id conflicts, heartbeat/TTL reaping, lifespan wiring."""
from __future__ import annotations

import asyncio
import textwrap

import pytest
from starlette.testclient import TestClient

from local_model_router.service.app import create_app
from local_model_router.service.swarm_gate import (
    CapacitySnapshot,
    SwarmError,
    SwarmPolicy,
    SwarmTerminalRegistry,
    SwarmTerminalSpec,
)

MODEL = "qwen3.5:9b"


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


async def _yielding_capacity() -> CapacitySnapshot:
    await asyncio.sleep(0.01)  # every spawn suspends here, so concurrent spawns interleave
    return CapacitySnapshot(available_models=[MODEL], main_vram_occupancy=0.1, free_vram_gb=20.0)


def _registry(clock: Clock, **policy) -> SwarmTerminalRegistry:
    return SwarmTerminalRegistry(SwarmPolicy(enabled=True, **policy), _yielding_capacity, clock=clock)


def _spec(**kw) -> SwarmTerminalSpec:
    return SwarmTerminalSpec(base_url="http://127.0.0.1:11434/v1", model=MODEL, **kw)


def test_concurrent_spawns_never_exceed_slots():
    async def run():
        reg = _registry(Clock(), max_swarm_slots=2)
        results = await asyncio.gather(*(reg.spawn(_spec()) for _ in range(10)), return_exceptions=True)
        granted = [r for r in results if not isinstance(r, BaseException)]
        errors = [r for r in results if isinstance(r, BaseException)]
        assert len(granted) == 2 and len({t.terminal_id for t in granted}) == 2
        assert len(errors) == 8
        assert all(isinstance(e, SwarmError) and e.code == "swarm_slots_full" for e in errors)
        assert reg.snapshot()["running"] == 2

    asyncio.run(run())


def test_reused_terminal_id_is_rejected_not_overwritten():
    async def run():
        reg = _registry(Clock(), max_swarm_slots=3)
        await reg.spawn(_spec(terminal_id="reviewer", task_meta={"n": 1}))
        with pytest.raises(SwarmError) as err:
            await reg.spawn(_spec(terminal_id="reviewer", task_meta={"n": 2}))
        assert (err.value.code, err.value.status_code) == ("terminal_id_conflict", 409)
        live = reg.snapshot()
        assert live["running"] == 1 and live["terminals"][0]["task_meta"] == {"n": 1}

    asyncio.run(run())


def test_heartbeat_keeps_live_terminal_and_reaped_one_gets_404():
    async def run():
        clock = Clock()
        reg = _registry(clock, default_ttl_seconds=30)
        term = await reg.spawn(_spec())
        for _ in range(5):  # 100s alive on a 30s TTL, heartbeating every 20s
            clock.t += 20
            assert await reg.heartbeat(term.terminal_id) is True
            assert term.terminal_id not in await reg.sweep()
        clock.t += 31
        assert term.terminal_id in await reg.sweep()
        assert await reg.heartbeat(term.terminal_id) is False

    asyncio.run(run())


def test_client_ttl_is_capped_by_policy():
    async def run():
        reg = _registry(Clock(), default_ttl_seconds=60)
        assert (await reg.spawn(_spec(ttl_seconds=10**9))).ttl_seconds == 60
        assert (await reg.spawn(_spec(ttl_seconds=15))).ttl_seconds == 15

    asyncio.run(run())


def test_reaper_does_not_charge_a_clock_jump_to_live_terminals():
    async def run():
        clock = Clock()
        reg = _registry(clock, default_ttl_seconds=30)
        term = await reg.spawn(_spec())
        reaper = asyncio.create_task(reg.sweep_forever(interval=0.01))
        try:
            await asyncio.sleep(0.05)
            clock.t += 8 * 3600  # host slept overnight / event loop was blocked
            await asyncio.sleep(0.05)
            assert await reg.heartbeat(term.terminal_id) is True
        finally:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)

    asyncio.run(run())


def test_reaper_survives_a_failing_sweep():
    async def run():
        reg = _registry(Clock())
        calls: list = []
        real_sweep = reg.sweep

        async def flaky(now=None):
            calls.append(now)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return await real_sweep(now)

        reg.sweep = flaky  # type: ignore[method-assign]
        reaper = asyncio.create_task(reg.sweep_forever(interval=0.01))
        try:
            await asyncio.sleep(0.06)
            assert len(calls) >= 2 and not reaper.done()
        finally:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)

    asyncio.run(run())


_FLEET = textwrap.dedent("""
    active_slots:
      - id: slot_router
        host: localhost
        port: 8080
        role: chat
        enabled: true
        router_mode: true
    global:
      backend: remote
""")

_UPSTREAMS = textwrap.dedent("""
    upstreams:
      - name: capped
        type: openai_compatible
        base_url: http://localhost:9999/v1
        enabled: true
""")


def test_app_lifespan_starts_reaper_and_serves_heartbeat(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    (tmp_path / "swarm.yaml").write_text("enabled: true\n", encoding="utf-8")
    (tmp_path / "llama_cpp_servers.yaml").write_text(_FLEET, encoding="utf-8")
    (tmp_path / "upstreams.yaml").write_text(_UPSTREAMS, encoding="utf-8")
    app = create_app(
        str(tmp_path / "llama_cpp_servers.yaml"),
        upstreams_path=str(tmp_path / "upstreams.yaml"),
        apps_path=str(tmp_path / "missing-apps.yaml"),
    )
    # `with` runs the lifespan, where the reaper starts. A bare TestClient(app) skips it.
    with TestClient(app) as client:
        assert client.get("/v1/swarm/capacity").json()["enabled"] is True
        resp = client.post("/v1/swarm/terminals/nope/heartbeat")
        assert resp.status_code == 404 and resp.json()["error"] == "terminal_not_found"
