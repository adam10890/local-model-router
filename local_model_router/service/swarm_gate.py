"""Swarm Terminal Gateway — the resource plane for sub-agent terminals.

The main agent (Hermes) is the *orchestrator*: it decides WHAT to do. This
module is the *resource plane*: it decides whether a new sub-agent terminal may
run right now, on what pinned model, and kills zombies that stop heartbeating.
It never decides what a sub does.

MVP isolation = **logical terminal**: a registry slot with a pinned model +
base_url, not a process/Docker per sub (process-per-terminal is Phase 3). The
router stays a capacity broker, not the swarm's orchestrator.

Feature flag = off by default. Enable by dropping a valid ``conf/swarm.yaml``
with ``enabled: true``.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

# A terminal occupies a slot from grant until removal. "pending" counts too, or a
# slow-starting sub (Phase 3) would let the next spawn over-subscribe.
_RUNNING_STATES = frozenset({"pending", "ready", "running", "idle"})
# Clock used everywhere so tests can freeze time.
Monotonic = Callable[[], float]
# CapacityProvider returns a live fleet snapshot on each spawn.
CapacityProvider = Callable[[], Awaitable["CapacitySnapshot"]]


class TerminalState(str, Enum):
    pending = "pending"
    ready = "ready"
    running = "running"
    idle = "idle"
    releasing = "releasing"
    dead = "dead"


class SwarmError(Exception):
    """A spawn was rejected. ``code`` is a stable machine id, ``status_code`` the HTTP mapping."""

    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message


class SwarmPolicyError(SwarmError):
    pass


class CapacitySnapshot(BaseModel):
    """What the registry needs to decide a spawn: models + main-model headroom."""

    model_config = ConfigDict(extra="forbid")

    available_models: list[str]
    main_vram_occupancy: Optional[float] = None  # 0.0-1.0, None = unknown
    free_vram_gb: float = 0.0


class SwarmPolicy(BaseModel):
    """Local heuristic policy. Works offline (no Jev decision model needed)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_swarm_slots: int = Field(default=2, ge=0)
    # Deny new subs once the main model's VRAM occupancy hits this fraction.
    main_pressure_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    # VRAM floor the main model keeps; subs never draw below it.
    reserve_for_main_vram_gb: float = 0.0
    # A sub that stops heartbeating longer than this is reaped.
    default_ttl_seconds: int = Field(default=900, ge=1)
    # max_depth=1 => subs can't spawn subs.
    max_depth: int = Field(default=1, ge=1)


class SwarmTerminalSpec(BaseModel):
    """Request body for POST /v1/swarm/terminals."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1)  # where the sub points its OpenAI client
    model: str = Field(min_length=1)  # pinned model id
    terminal_id: Optional[str] = None
    workdir: Optional[str] = None
    parent_session_id: Optional[str] = None
    ttl_seconds: Optional[int] = Field(default=None, ge=1)  # capped at the policy TTL
    depth: int = Field(default=1, ge=1)
    task_meta: dict[str, Any] = Field(default_factory=dict)
    callback_url: Optional[str] = None


class SwarmTerminal(BaseModel):
    """One live logical terminal in the registry."""

    model_config = ConfigDict(extra="forbid")

    terminal_id: str
    base_url: str
    model: str
    workdir: Optional[str] = None
    parent_session_id: Optional[str] = None
    status: str = TerminalState.running.value
    depth: int = 1
    resources: dict[str, Any] = Field(default_factory=dict)
    created_at: float
    last_heartbeat: float
    ttl_seconds: int
    task_meta: dict[str, Any] = Field(default_factory=dict)
    callback_url: Optional[str] = None

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def load_swarm_policy(path: str | Path) -> SwarmPolicy:
    """Load policy from ``conf/swarm.yaml``; defaults (disabled) when absent/broken."""
    source = Path(path)
    if not source.is_file():
        return SwarmPolicy()
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Swarm policy ignored (could not read %s): %s", source, exc)
        return SwarmPolicy()
    if not isinstance(raw, dict):
        logger.warning("Swarm policy ignored (%s: top level must be a mapping)", source)
        return SwarmPolicy()
    try:
        return SwarmPolicy.model_validate(raw)
    except ValidationError as exc:
        logger.warning("Swarm policy ignored (%s): %s", source, exc.message)
        return SwarmPolicy()


class SwarmTerminalRegistry:
    """Logical-terminal registry: capacity broker + zombie reaper.

    Not the swarm's orchestrator. It only answers "may this terminal run now?"
    and frees/kills slots. The main agent still decides what each sub does.
    """

    def __init__(
        self,
        policy: SwarmPolicy,
        capacity_provider: CapacityProvider,
        *,
        clock: Monotonic = time.monotonic,
    ) -> None:
        self._policy = policy
        self._capacity = capacity_provider
        self._clock = clock
        self._terminals: dict[str, SwarmTerminal] = {}
        self._lock = asyncio.Lock()

    async def spawn(self, spec: SwarmTerminalSpec) -> SwarmTerminal:
        if not self._policy.enabled:
            raise SwarmPolicyError(
                "swarm_disabled",
                403,
                "swarm gateway is disabled; set enabled: true in conf/swarm.yaml",
            )
        if spec.depth > self._policy.max_depth:
            raise SwarmPolicyError(
                "depth_exceeded",
                400,
                f"max swarm depth is {self._policy.max_depth}; refused depth {spec.depth}",
            )

        # Outside the lock on purpose: VRAM is a point-in-time gate, not a reservation
        # (a granted sub uses nothing yet). The slot count below is the atomic part.
        cap = await self._capacity()

        async with self._lock:
            # Same lock as the insert: a reused id would overwrite a live slot and
            # leave two subs on one ledger entry (silent over-subscription).
            terminal_id = (spec.terminal_id or "").strip() or f"swarm-{uuid.uuid4().hex[:12]}"
            if terminal_id in self._terminals:
                raise SwarmError(
                    "terminal_id_conflict",
                    409,
                    f"terminal '{terminal_id}' is live; release it or pick another id",
                )
            if spec.model not in cap.available_models:
                raise SwarmError(
                    "model_unavailable",
                    400,
                    f"model '{spec.model}' is not available on the fleet",
                )
            running = [t for t in self._terminals.values() if t.status in _RUNNING_STATES]
            if len(running) >= self._policy.max_swarm_slots:
                raise SwarmError(
                    "swarm_slots_full",
                    503,
                    f"swarm is full ({self._policy.max_swarm_slots} slots)",
                )
            occ = cap.main_vram_occupancy
            if occ is not None and occ >= self._policy.main_pressure_threshold:
                raise SwarmError(
                    "main_under_pressure",
                    503,
                    f"main-model VRAM occupancy {occ:.0%} >= {self._policy.main_pressure_threshold:.0%}",
                )
            if cap.free_vram_gb < self._policy.reserve_for_main_vram_gb:
                raise SwarmError(
                    "vram_reserved_for_main",
                    503,
                    f"only {cap.free_vram_gb:.1f} GB free; "
                    f"{self._policy.reserve_for_main_vram_gb:.1f} GB reserved for the main model",
                )

            now = self._clock()
            # Policy TTL is the ceiling: a client may shorten it, never opt out of reaping.
            ttl = min(spec.ttl_seconds or self._policy.default_ttl_seconds, self._policy.default_ttl_seconds)
            terminal = SwarmTerminal(
                terminal_id=terminal_id,
                base_url=spec.base_url,
                model=spec.model,
                workdir=spec.workdir,
                parent_session_id=spec.parent_session_id,
                status=TerminalState.ready.value,
                depth=spec.depth,
                resources={
                    "free_vram_gb": round(cap.free_vram_gb, 2),
                    "main_vram_occupancy": cap.main_vram_occupancy,
                    "available_models": cap.available_models,
                },
                created_at=now,
                last_heartbeat=now,
                ttl_seconds=ttl,
                task_meta=spec.task_meta,
                callback_url=spec.callback_url,
            )
            self._terminals[terminal.terminal_id] = terminal
            logger.info("Swarm: spawned terminal %s (model=%s depth=%d)", terminal.terminal_id, terminal.model, terminal.depth)
            return terminal

    async def release(self, terminal_id: str) -> bool:
        async with self._lock:
            terminal = self._terminals.get(terminal_id)
            if terminal is None:
                return False
            terminal.status = TerminalState.releasing.value
            del self._terminals[terminal_id]
            logger.info("Swarm: released terminal %s", terminal_id)
            return True

    async def heartbeat(self, terminal_id: str) -> bool:
        async with self._lock:
            terminal = self._terminals.get(terminal_id)
            if terminal is None:
                return False
            terminal.last_heartbeat = self._clock()
            return True

    async def sweep(self, now: Optional[float] = None) -> list[str]:
        """Reap terminals past their TTL since last heartbeat. Returns dead ids."""
        now = self._clock() if now is None else now
        dead: list[str] = []
        async with self._lock:
            for terminal_id, terminal in list(self._terminals.items()):
                if terminal.status in {TerminalState.releasing.value, TerminalState.dead.value}:
                    dead.append(terminal_id)
                    del self._terminals[terminal_id]
                    continue
                if now - terminal.last_heartbeat > terminal.ttl_seconds:
                    dead.append(terminal_id)
                    del self._terminals[terminal_id]
                    logger.info("Swarm: reaped zombie terminal %s", terminal_id)
            return dead

    async def sweep_forever(self, interval: float = 15.0) -> None:
        """Reaper loop, started by the app lifespan when the flag is on.

        Must never die: a dead reaper leaks every slot until restart. Time the loop
        could not observe (host sleep, a blocked event loop) is not charged to
        terminals; they could not heartbeat during it either.
        """
        last = self._clock()
        while True:
            await asyncio.sleep(interval)
            now = self._clock()
            stalled = now - last - interval
            last = now
            try:
                if stalled > 2 * interval:
                    logger.warning("Swarm: reaper stalled %.0fs; not charging it to terminals", stalled)
                    async with self._lock:
                        for terminal in self._terminals.values():
                            terminal.last_heartbeat = min(now, terminal.last_heartbeat + stalled)
                await self.sweep(now)
            except Exception:
                logger.exception("Swarm: sweep failed; reaper keeps running")

    def snapshot(self) -> dict[str, Any]:
        running = [t for t in self._terminals.values() if t.status in _RUNNING_STATES]
        return {
            "enabled": self._policy.enabled,
            "running": len(running),
            "max_swarm_slots": self._policy.max_swarm_slots,
            "terminals": [t.public_dict() for t in running],
        }


def build_registry(
    policy: SwarmPolicy,
    capacity_provider: CapacityProvider,
    *,
    clock: Monotonic = time.monotonic,
) -> SwarmTerminalRegistry:
    return SwarmTerminalRegistry(policy, capacity_provider, clock=clock)


async def _demo() -> None:
    """Smallest thing that fails if the lifecycle/capacity/zombie logic breaks."""
    frozen = {"t": 1000.0}

    def clock() -> float:
        return frozen["t"]

    class FakeCap:
        async def __call__(self) -> CapacitySnapshot:
            return CapacitySnapshot(
                available_models=["qwen3.5:9b", "nomic-embed"],
                main_vram_occupancy=0.5,
                free_vram_gb=8.0,
            )

    policy = SwarmPolicy(enabled=True, max_swarm_slots=2)
    reg = build_registry(policy, FakeCap(), clock=clock)

    s1 = await reg.spawn(SwarmTerminalSpec(base_url="http://127.0.0.1:8000/v1", model="qwen3.5:9b", task_meta={"task": "review"}))
    assert s1.status == TerminalState.ready.value and s1.depth == 1

    s2 = await reg.spawn(SwarmTerminalSpec(base_url="http://127.0.0.1:8000/v1", model="nomic-embed"))
    assert reg.snapshot()["running"] == 2

    # third spawn over the 2-slot cap
    try:
        await reg.spawn(SwarmTerminalSpec(base_url="http://127.0.0.1:8000/v1", model="qwen3.5:9b"))
        raise AssertionError("expected swarm_slots_full")
    except SwarmError as exc:
        assert exc.code == "swarm_slots_full"

    # unknown model rejected
    try:
        await reg.spawn(SwarmTerminalSpec(base_url="http://127.0.0.1:8000/v1", model="nope"))
        raise AssertionError("expected model_unavailable")
    except SwarmError as exc:
        assert exc.code == "model_unavailable"

    # heartbeat keeps s1 alive past an old clock; s2 (no heartbeat) is reaped
    frozen["t"] += 10_000
    assert await reg.heartbeat(s1.terminal_id) is True
    dead = await reg.sweep()
    assert s1.terminal_id not in dead and s2.terminal_id in dead

    # release + release-unknown
    assert await reg.release(s1.terminal_id) is True
    assert await reg.release("does-not-exist") is False

    # disabled policy refuses everything
    off = build_registry(SwarmPolicy(enabled=False), FakeCap(), clock=clock)
    try:
        await off.spawn(SwarmTerminalSpec(base_url="http://127.0.0.1:8000/v1", model="qwen3.5:9b"))
        raise AssertionError("expected swarm_disabled")
    except SwarmError as exc:
        assert exc.code == "swarm_disabled"

    print("swarm_gate self-check OK")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_demo())
