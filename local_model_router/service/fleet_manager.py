"""Fleet Manager state, agent identity, and admission control.

This module is intentionally Docker-free. It tracks who is using the local
fleet and gates concurrent upstream requests, but container control belongs to
a future host-side worker.
"""
from __future__ import annotations

import asyncio
import configparser
import json
import os
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

try:
    from local_model_router.helpers.context_planner import (
        effective_ratio_for_role,
        context_status_from_slot,
        vram_safety_margin_gb,
    )
except ImportError:  # pragma: no cover - standalone service import mode
    from local_model_router.helpers.context_planner import (  # type: ignore
        effective_ratio_for_role,
        context_status_from_slot,
        vram_safety_margin_gb,
    )

VALID_PRIORITIES = {"low", "normal", "high"}
_PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2}
_STATE_DB_ENV = "A0_FLEET_STATE_DB"
_MAX_ACTIVE_ENV = "A0_FLEET_MAX_ACTIVE"
_MAX_QUEUE_ENV = "A0_FLEET_MAX_QUEUE"


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str = "anonymous"
    agent_type: str = "custom"
    priority: str = "normal"

    def as_dict(self) -> Dict[str, str]:
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class Admission:
    queued_ms: int
    queue_depth_at_admit: int
    active_at_admit: int


class QueueFull(Exception):
    def __init__(self, queue_depth: int, max_queue: int) -> None:
        super().__init__("fleet queue is full")
        self.queue_depth = queue_depth
        self.max_queue = max_queue


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _default_db_path() -> str:
    base = Path(tempfile.gettempdir()) / "a0_lmm_router"
    return str(base / "fleet_state.sqlite3")


def _int_from_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, "").strip())
    except Exception:
        return default
    return max(minimum, value)


def normalize_priority(value: object) -> str:
    priority = str(value or "normal").strip().lower()
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"invalid priority '{priority}'")
    return priority


def identity_from_headers(headers: Any, fallback: Optional[Dict[str, Any]] = None) -> AgentIdentity:
    """Resolve agent identity from HTTP headers, then optional body metadata."""
    fallback = fallback or {}
    routing = fallback.get("routing") if isinstance(fallback.get("routing"), dict) else {}
    metadata = fallback.get("metadata") if isinstance(fallback.get("metadata"), dict) else {}

    def pick(header: str, key: str, default: str) -> str:
        value = headers.get(header) if headers is not None else None
        if value:
            return str(value).strip()
        if key in routing and routing[key]:
            return str(routing[key]).strip()
        if key in fallback and fallback[key]:
            return str(fallback[key]).strip()
        if key in metadata and metadata[key]:
            return str(metadata[key]).strip()
        return default

    agent_id = pick("x-agent-id", "agent_id", "anonymous") or "anonymous"
    agent_type = pick("x-agent-type", "agent_type", "custom") or "custom"
    priority = normalize_priority(pick("x-priority", "priority", "normal"))
    return AgentIdentity(agent_id=agent_id, agent_type=agent_type, priority=priority)


class FleetStore:
    """Small SQLite-backed state store for Fleet Manager telemetry."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or os.environ.get(_STATE_DB_ENV, "").strip() or _default_db_path()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    agent_type TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    agent_type TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    status TEXT NOT NULL,
                    slot_id TEXT,
                    model TEXT,
                    queued_ms INTEGER,
                    duration_ms INTEGER,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS queue_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT,
                    agent_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    queue_depth INTEGER NOT NULL,
                    active_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_residency_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def upsert_agent(self, agent: AgentIdentity, metadata: Optional[Dict[str, Any]] = None) -> None:
        metadata_json = _json_dumps(metadata or {})
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agents (
                    agent_id, agent_type, priority, metadata_json,
                    first_seen_at, last_seen_at, request_count
                )
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(agent_id) DO UPDATE SET
                    agent_type=excluded.agent_type,
                    priority=excluded.priority,
                    metadata_json=excluded.metadata_json,
                    last_seen_at=excluded.last_seen_at
                """,
                (agent.agent_id, agent.agent_type, agent.priority, metadata_json, now, now),
            )

    def register_agent(self, agent: AgentIdentity, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.upsert_agent(agent, metadata=metadata)
        return self.get_agent(agent.agent_id) or agent.as_dict()

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        return self._agent_row(row) if row else None

    def list_agents(self) -> list[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM agents ORDER BY last_seen_at DESC, agent_id ASC").fetchall()
        return [self._agent_row(row) for row in rows]

    def _agent_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            metadata = {}
        return {
            "agent_id": row["agent_id"],
            "agent_type": row["agent_type"],
            "priority": row["priority"],
            "metadata": metadata,
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "request_count": row["request_count"],
        }

    def create_request(self, agent: AgentIdentity) -> str:
        request_id = f"req_{uuid.uuid4().hex}"
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO requests (
                    request_id, agent_id, agent_type, priority, status,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'created', ?, ?)
                """,
                (request_id, agent.agent_id, agent.agent_type, agent.priority, now, now),
            )
            conn.execute(
                """
                INSERT INTO agents (
                    agent_id, agent_type, priority, metadata_json,
                    first_seen_at, last_seen_at, request_count
                )
                VALUES (?, ?, ?, '{}', ?, ?, 1)
                ON CONFLICT(agent_id) DO UPDATE SET
                    agent_type=excluded.agent_type,
                    priority=excluded.priority,
                    last_seen_at=excluded.last_seen_at,
                    request_count=request_count + 1
                """,
                (agent.agent_id, agent.agent_type, agent.priority, now, now),
            )
        return request_id

    def update_request(
        self,
        request_id: str,
        *,
        status: str,
        slot_id: Optional[str] = None,
        model: Optional[str] = None,
        queued_ms: Optional[int] = None,
        duration_ms: Optional[int] = None,
        error_code: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE requests
                SET status = ?,
                    slot_id = COALESCE(?, slot_id),
                    model = COALESCE(?, model),
                    queued_ms = COALESCE(?, queued_ms),
                    duration_ms = COALESCE(?, duration_ms),
                    error_code = COALESCE(?, error_code),
                    updated_at = ?
                WHERE request_id = ?
                """,
                (status, slot_id, model, queued_ms, duration_ms, error_code, _now_iso(), request_id),
            )

    def record_queue_event(
        self,
        *,
        request_id: Optional[str],
        agent: AgentIdentity,
        event_type: str,
        queue_depth: int,
        active_count: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO queue_events (
                    request_id, agent_id, event_type, queue_depth, active_count, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (request_id, agent.agent_id, event_type, queue_depth, active_count, _now_iso()),
            )

    def record_model_snapshot(self, source: str, payload: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO model_residency_snapshots (source, payload_json, created_at)
                VALUES (?, ?, ?)
                """,
                (source, _json_dumps(payload), _now_iso()),
            )

    def request_summary(self) -> Dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"]
            by_status_rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM requests GROUP BY status ORDER BY status"
            ).fetchall()
            recent_rows = conn.execute(
                """
                SELECT request_id, agent_id, priority, status, slot_id, model, error_code, updated_at
                FROM requests
                ORDER BY updated_at DESC
                LIMIT 10
                """
            ).fetchall()
        return {
            "total": total,
            "by_status": {row["status"]: row["n"] for row in by_status_rows},
            "recent": [dict(row) for row in recent_rows],
        }


class FleetQueue:
    """Bounded async admission controller.

    It limits concurrent upstream requests and caps waiting requests. High
    priority requests move ahead of lower priority waiters, but running work is
    never preempted.
    """

    def __init__(self, max_active: Optional[int] = None, max_queue: Optional[int] = None) -> None:
        self.max_active = max_active if max_active is not None else _int_from_env(_MAX_ACTIVE_ENV, 1, minimum=1)
        self.max_queue = max_queue if max_queue is not None else _int_from_env(_MAX_QUEUE_ENV, 32, minimum=0)
        self._active = 0
        self._sequence = 0
        self._waiters: list[tuple[int, int, asyncio.Future[None], float]] = []
        self._lock = asyncio.Lock()

    def snapshot(self) -> Dict[str, int]:
        return {
            "active": self._active,
            "queued": len(self._waiters),
            "max_active": self.max_active,
            "max_queue": self.max_queue,
        }

    async def acquire(self, priority: str) -> Admission:
        priority = normalize_priority(priority)
        started = _now()
        async with self._lock:
            if self._active < self.max_active and not self._waiters:
                self._active += 1
                return Admission(queued_ms=0, queue_depth_at_admit=0, active_at_admit=self._active)

            if len(self._waiters) >= self.max_queue:
                raise QueueFull(queue_depth=len(self._waiters), max_queue=self.max_queue)

            loop = asyncio.get_running_loop()
            future: asyncio.Future[None] = loop.create_future()
            self._sequence += 1
            rank = _PRIORITY_RANK[priority]
            self._waiters.append((rank, self._sequence, future, started))
            self._waiters.sort(key=lambda item: (-item[0], item[1]))

        await future
        queued_ms = int((_now() - started) * 1000)
        async with self._lock:
            return Admission(
                queued_ms=queued_ms,
                queue_depth_at_admit=len(self._waiters),
                active_at_admit=self._active,
            )

    async def release(self) -> None:
        async with self._lock:
            if self._active > 0:
                self._active -= 1
            while self._waiters:
                _rank, _seq, future, _started = self._waiters.pop(0)
                if future.cancelled():
                    continue
                self._active += 1
                future.set_result(None)
                break


def fleet_config_from_env() -> Dict[str, Any]:
    return {
        "state_db": os.environ.get(_STATE_DB_ENV, "").strip() or _default_db_path(),
        "max_active": _int_from_env(_MAX_ACTIVE_ENV, 1, minimum=1),
        "max_queue": _int_from_env(_MAX_QUEUE_ENV, 32, minimum=0),
        "effective_ctx_ratio": effective_ratio_for_role("chat"),
        "vram_safety_margin_gb": vram_safety_margin_gb(),
        "docker_socket_enabled": False,
    }


def vram_unknown_summary() -> Dict[str, Any]:
    return {
        "total_gb": None,
        "used_gb": None,
        "available_gb": None,
        "source": "not_configured",
    }


def slots_model_snapshot(slots: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    snapshot = []
    for slot in slots:
        context = context_status_from_slot(slot)
        snapshot.append(
            {
                "slot_id": slot.get("id"),
                "role": slot.get("role"),
                "model_id": slot.get("model_id"),
                "enabled": slot.get("enabled"),
                "base_url": slot.get("base_url"),
                "context": context,
            }
        )
    return snapshot


def _role_from_alias(alias: str, fallback: str = "chat") -> str:
    value = (alias or "").strip().lower()
    if value in {"embedding", "embed"}:
        return "embed"
    if value == "utility":
        return "utility"
    if value == "chat":
        return "chat"
    return fallback or "chat"


def _local_preset_path(raw_path: str) -> Optional[Path]:
    path = (raw_path or "").strip()
    if not path:
        return None
    if path == "/etc/llama/preset.ini":
        return Path(__file__).resolve().parents[1] / "conf" / "models_preset.ini"
    candidate = Path(path)
    return candidate if candidate.is_file() else None


def _context_windows_from_preset(slot: Dict[str, Any]) -> list[Dict[str, Any]]:
    preset_path = _local_preset_path(str(slot.get("router_models_preset") or ""))
    if not preset_path or not preset_path.is_file():
        return []
    parser = configparser.ConfigParser()
    parser.read(preset_path, encoding="utf-8")
    windows: list[Dict[str, Any]] = []
    for section in parser.sections():
        if section == "*":
            continue
        alias = parser.get(section, "alias", fallback=section)
        role = _role_from_alias(alias, str(slot.get("role") or "chat"))
        hard_ctx = parser.getint(
            section,
            "ctx-size",
            fallback=parser.getint(section, "ctx_size", fallback=int(slot.get("context_size") or 0)),
        )
        effective = int(hard_ctx * effective_ratio_for_role(role)) if hard_ctx else None
        windows.append(
            {
                "slot_id": slot.get("id"),
                "alias": alias,
                "role": role,
                "model_id": alias,
                "model_path": parser.get(section, "model", fallback=""),
                "min_ctx": context_status_from_slot({"role": role}).get("min_ctx"),
                "hard_ctx": hard_ctx,
                "effective_ctx": effective,
                "response_reserve": context_status_from_slot({"role": role}).get("response_reserve"),
                "effective_ratio": effective_ratio_for_role(role),
                "occupancy": None,
                "resident": None,
                "planned_vram_gb": None,
            }
        )
    return windows


def context_windows_from_slots(slots: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    windows: list[Dict[str, Any]] = []
    for slot in slots:
        if slot.get("router_mode"):
            alias_windows = _context_windows_from_preset(slot)
            if alias_windows:
                windows.extend(alias_windows)
                continue
        windows.append(context_status_from_slot(slot))
    return windows
