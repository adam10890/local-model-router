"""Observe-first ticket orchestration for local multi-agent work.

This module coordinates work packets for external/sub-agent runners. It does
not start containers, edit project files, or mutate DOX documents directly.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import yaml


WORKSPACE_ENV = "A0_AGENT_ORCH_DIR"
DB_ENV = "A0_AGENT_ORCH_DB"
DEFAULT_MODEL_HINT = "WeiboAI/VibeThinker-3B"

TICKET_STATUSES = {"pending", "ready", "running", "completed", "blocked", "failed"}
FINAL_TICKET_STATUSES = {"completed", "blocked", "failed"}
PLAN_STATUSES = {"open", "ready_for_planner", "needs_attention"}


class OrchestratorError(Exception):
    """Structured validation or lookup error for the HTTP layer."""

    def __init__(self, message: str, code: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _default_workspace_root() -> str:
    return str(Path(tempfile.gettempdir()) / "a0_lmm_router" / "agent_orchestrator")


def _safe_id(prefix: str, provided: object = None) -> str:
    raw = str(provided or "").strip()
    if not raw:
        return f"{prefix}_{uuid.uuid4().hex}"
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", raw):
        raise OrchestratorError(
            f"invalid id '{raw}'; use letters, numbers, '.', '_' or '-'",
            "invalid_id",
            422,
        )
    return raw


def _list_of_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


class AgentOrchestrator:
    """SQLite + filesystem ticket coordinator.

    The database owns discoverable state. The workspace owns durable packet
    files for agents that communicate through folders.
    """

    def __init__(
        self,
        *,
        db_path: Optional[str] = None,
        workspace_root: Optional[str] = None,
        repo_root: Optional[str] = None,
        bundles_path: Optional[str] = None,
    ) -> None:
        self.workspace_root = str(
            Path(workspace_root or os.environ.get(WORKSPACE_ENV, "").strip() or _default_workspace_root())
        )
        self.db_path = db_path or os.environ.get(DB_ENV, "").strip() or str(
            Path(self.workspace_root) / "orchestrator.sqlite3"
        )
        self.repo_root = Path(repo_root or Path(__file__).resolve().parents[2]).resolve()
        self.bundles_path = str(bundles_path) if bundles_path else ""
        self._default_bundles = self._load_bundles(self.bundles_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        Path(self.workspace_root).mkdir(parents=True, exist_ok=True)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    goal TEXT NOT NULL,
                    planner_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    workspace_path TEXT NOT NULL,
                    work_path TEXT NOT NULL,
                    compose_path TEXT NOT NULL,
                    wake_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    agent_role TEXT NOT NULL,
                    model_hint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    required INTEGER NOT NULL,
                    dependencies_json TEXT NOT NULL DEFAULT '[]',
                    target_paths_json TEXT NOT NULL DEFAULT '[]',
                    capability_bundles_json TEXT NOT NULL DEFAULT '[]',
                    resolved_capabilities_json TEXT NOT NULL DEFAULT '{}',
                    docker_json TEXT NOT NULL DEFAULT '{}',
                    workspace_path TEXT NOT NULL,
                    task_path TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    dox_chain_path TEXT NOT NULL,
                    dox_report_path TEXT NOT NULL,
                    summary TEXT,
                    dox_unchanged_reason TEXT,
                    handoff_to TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
                );

                CREATE TABLE IF NOT EXISTS ticket_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id TEXT NOT NULL,
                    ticket_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )

    def _load_bundles(self, path: str) -> dict[str, dict[str, list[str]]]:
        if not path:
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            return {}
        except Exception:
            return {}
        bundles = data.get("bundles") if isinstance(data, dict) else {}
        if not isinstance(bundles, dict):
            return {}
        return {str(name): self._normalize_capability_bundle(value) for name, value in bundles.items()}

    @staticmethod
    def _normalize_capability_bundle(value: Any) -> dict[str, list[str]]:
        source = value if isinstance(value, dict) else {}
        return {
            "mcp_servers": _list_of_strings(source.get("mcp_servers")),
            "plugins": _list_of_strings(source.get("plugins")),
            "tools": _list_of_strings(source.get("tools")),
        }

    def _bundles_for_request(self, body: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
        bundles = dict(self._default_bundles)
        inline = body.get("capability_bundle_config")
        if inline is None and isinstance(body.get("capability_bundles"), dict):
            inline = body.get("capability_bundles")
        if isinstance(inline, dict):
            for name, value in inline.items():
                bundles[str(name)] = self._normalize_capability_bundle(value)
        return bundles

    @staticmethod
    def _resolve_capabilities(
        requested: Iterable[str],
        bundles: dict[str, dict[str, list[str]]],
    ) -> dict[str, Any]:
        resolved = {"mcp_servers": [], "plugins": [], "tools": []}
        for bundle_name in requested:
            bundle = bundles.get(bundle_name, {})
            for key in resolved:
                for item in bundle.get(key, []):
                    if item not in resolved[key]:
                        resolved[key].append(item)
        return {"requested_bundles": list(requested), "resolved": resolved}

    def create_plan(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise OrchestratorError("request body must be a JSON object", "invalid_request_body", 400)

        goal = str(body.get("goal") or "").strip()
        if not goal:
            raise OrchestratorError("missing required field: goal", "missing_goal", 422)
        tickets_input = body.get("tickets")
        if not isinstance(tickets_input, list) or not tickets_input:
            raise OrchestratorError("missing required field: tickets", "missing_tickets", 422)

        plan_id = _safe_id("plan", body.get("plan_id"))
        plan_dir = Path(self.workspace_root) / "plans" / plan_id
        tickets_dir = plan_dir / "tickets"
        tickets_dir.mkdir(parents=True, exist_ok=False)

        now = _now_iso()
        work_document = str(body.get("work_document") or "")
        work_path = plan_dir / "work.md"
        plan_json_path = plan_dir / "plan.json"
        compose_path = plan_dir / "compose.plan.yaml"
        work_path.write_text(work_document, encoding="utf-8")

        bundles = self._bundles_for_request(body)
        planner = body.get("planner") if isinstance(body.get("planner"), dict) else {}
        conversation_id = str(body.get("conversation_id") or "").strip() or None
        ticket_rows: list[dict[str, Any]] = []

        for index, raw_ticket in enumerate(tickets_input, start=1):
            if not isinstance(raw_ticket, dict):
                raise OrchestratorError("each ticket must be a JSON object", "invalid_ticket", 422)
            ticket_id = _safe_id("ticket", raw_ticket.get("id") or raw_ticket.get("ticket_id"))
            title = str(raw_ticket.get("title") or ticket_id).strip()
            prompt = str(raw_ticket.get("prompt") or "").strip()
            if not prompt:
                raise OrchestratorError(f"ticket '{ticket_id}' is missing prompt", "missing_ticket_prompt", 422)
            dependencies = _list_of_strings(raw_ticket.get("dependencies"))
            target_paths = _list_of_strings(raw_ticket.get("target_paths"))
            requested_bundles = _list_of_strings(raw_ticket.get("capability_bundles"))
            capabilities = self._resolve_capabilities(requested_bundles, bundles)
            docker = raw_ticket.get("docker") if isinstance(raw_ticket.get("docker"), dict) else {}
            model_hint = str(raw_ticket.get("model_hint") or DEFAULT_MODEL_HINT).strip() or DEFAULT_MODEL_HINT
            status = "pending" if dependencies else "ready"

            ticket_dir = tickets_dir / ticket_id
            artifact_dir = ticket_dir / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=False)
            task_path = ticket_dir / "task.md"
            ticket_path = ticket_dir / "ticket.json"
            capabilities_path = ticket_dir / "capabilities.json"
            dox_chain_path = ticket_dir / "dox_chain.md"
            dox_report_path = ticket_dir / "dox_report.md"

            task_path.write_text(
                self._render_task_md(
                    ticket_id=ticket_id,
                    title=title,
                    prompt=prompt,
                    agent_role=str(raw_ticket.get("agent_role") or "worker"),
                    model_hint=model_hint,
                    dependencies=dependencies,
                    target_paths=target_paths,
                ),
                encoding="utf-8",
            )
            dox_chain_path.write_text(self._render_dox_chain(target_paths), encoding="utf-8")
            dox_report_path.write_text("Pending sub-agent DOX response.\n", encoding="utf-8")

            ticket_doc = {
                "ticket_id": ticket_id,
                "plan_id": plan_id,
                "title": title,
                "agent_role": str(raw_ticket.get("agent_role") or "worker"),
                "model_hint": model_hint,
                "status": status,
                "required": bool(raw_ticket.get("required", True)),
                "dependencies": dependencies,
                "target_paths": target_paths,
                "capability_bundles": requested_bundles,
                "prompt_path": str(task_path),
                "artifact_path": str(artifact_dir),
                "dox_chain_path": str(dox_chain_path),
                "dox_report_path": str(dox_report_path),
                "docker": docker,
                "order": index,
            }
            ticket_path.write_text(_json_dumps(ticket_doc) + "\n", encoding="utf-8")
            capabilities_path.write_text(_json_dumps(capabilities) + "\n", encoding="utf-8")

            ticket_rows.append(
                {
                    **ticket_doc,
                    "workspace_path": str(ticket_dir),
                    "task_path": str(task_path),
                    "resolved_capabilities": capabilities,
                    "created_at": now,
                    "updated_at": now,
                }
            )

        plan_doc = {
            "plan_id": plan_id,
            "goal": goal,
            "conversation_id": conversation_id,
            "planner": planner,
            "status": "open",
            "workspace_path": str(plan_dir),
            "work_path": str(work_path),
            "compose_path": str(compose_path),
            "tickets": [
                {
                    "ticket_id": row["ticket_id"],
                    "title": row["title"],
                    "status": row["status"],
                    "task_path": row["task_path"],
                    "ticket_path": str(Path(row["workspace_path"]) / "ticket.json"),
                }
                for row in ticket_rows
            ],
        }
        plan_json_path.write_text(_json_dumps(plan_doc) + "\n", encoding="utf-8")
        compose_path.write_text(self._render_compose_plan(plan_id, ticket_rows), encoding="utf-8")

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO plans (
                    plan_id, conversation_id, goal, planner_json, status,
                    workspace_path, work_path, compose_path, wake_path,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'open', ?, ?, ?, NULL, ?, ?)
                """,
                (
                    plan_id,
                    conversation_id,
                    goal,
                    _json_dumps(planner),
                    str(plan_dir),
                    str(work_path),
                    str(compose_path),
                    now,
                    now,
                ),
            )
            for row in ticket_rows:
                conn.execute(
                    """
                    INSERT INTO tickets (
                        ticket_id, plan_id, title, agent_role, model_hint,
                        status, required, dependencies_json, target_paths_json,
                        capability_bundles_json, resolved_capabilities_json,
                        docker_json, workspace_path, task_path, artifact_path,
                        dox_chain_path, dox_report_path, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["ticket_id"],
                        plan_id,
                        row["title"],
                        row["agent_role"],
                        row["model_hint"],
                        row["status"],
                        int(bool(row["required"])),
                        _json_dumps(row["dependencies"]),
                        _json_dumps(row["target_paths"]),
                        _json_dumps(row["capability_bundles"]),
                        _json_dumps(row["resolved_capabilities"]),
                        _json_dumps(row["docker"]),
                        row["workspace_path"],
                        row["task_path"],
                        row["artifact_path"],
                        row["dox_chain_path"],
                        row["dox_report_path"],
                        now,
                        now,
                    ),
                )
            conn.execute(
                """
                INSERT INTO ticket_events (plan_id, ticket_id, event_type, payload_json, created_at)
                VALUES (?, NULL, 'plan_created', ?, ?)
                """,
                (plan_id, _json_dumps({"ticket_count": len(ticket_rows)}), now),
            )

        return self.get_plan(plan_id, include_work=False)

    @staticmethod
    def _render_task_md(
        *,
        ticket_id: str,
        title: str,
        prompt: str,
        agent_role: str,
        model_hint: str,
        dependencies: list[str],
        target_paths: list[str],
    ) -> str:
        deps = "\n".join(f"- {item}" for item in dependencies) or "- none"
        targets = "\n".join(f"- {item}" for item in target_paths) or "- none"
        return (
            f"# {title}\n\n"
            f"- Ticket: `{ticket_id}`\n"
            f"- Agent role: `{agent_role}`\n"
            f"- Model hint: `{model_hint}`\n\n"
            "## Dependencies\n\n"
            f"{deps}\n\n"
            "## Target Paths For DOX\n\n"
            f"{targets}\n\n"
            "## Prompt\n\n"
            f"{prompt}\n"
        )

    def _render_dox_chain(self, target_paths: list[str]) -> str:
        parts = ["# DOX Chain\n"]
        seen: set[Path] = set()
        candidates = target_paths or ["."]
        for target in candidates:
            parts.append(f"\n## Target `{target}`\n")
            for agents_path in self._agents_paths_for_target(target):
                if agents_path in seen:
                    continue
                seen.add(agents_path)
                parts.append(f"\n### {agents_path}\n\n")
                try:
                    parts.append(agents_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    parts.append(f"Could not read AGENTS.md: {exc}\n")
                if not parts[-1].endswith("\n"):
                    parts.append("\n")
        if not seen:
            parts.append("\nNo AGENTS.md files were found for the requested targets.\n")
        return "".join(parts)

    def _agents_paths_for_target(self, target: str) -> list[Path]:
        raw = Path(target)
        candidate = raw if raw.is_absolute() else self.repo_root / raw
        try:
            candidate = candidate.resolve()
        except Exception:
            candidate = self.repo_root
        target_dir = candidate.parent if candidate.suffix else candidate

        try:
            relative = target_dir.relative_to(self.repo_root)
        except ValueError:
            dirs = [self.repo_root]
        else:
            dirs = [self.repo_root]
            current = self.repo_root
            for part in relative.parts:
                current = current / part
                dirs.append(current)

        paths = []
        for directory in dirs:
            agents = directory / "AGENTS.md"
            if agents.is_file():
                paths.append(agents)
        return paths

    @staticmethod
    def _render_compose_plan(plan_id: str, tickets: list[dict[str, Any]]) -> str:
        services: dict[str, Any] = {}
        for ticket in tickets:
            docker = ticket.get("docker") if isinstance(ticket.get("docker"), dict) else {}
            service: dict[str, Any] = {
                "image": docker.get("image") or "local-model-router-agent:planned",
                "profiles": ["draft-only"],
                "environment": {
                    "A0_ORCH_PLAN_ID": plan_id,
                    "A0_ORCH_TICKET_ID": ticket["ticket_id"],
                    "A0_ORCH_WORKSPACE": ticket["workspace_path"],
                },
                "volumes": [f"{ticket['workspace_path']}:/workspace:rw"],
            }
            resources = {}
            for key in ("cpus", "memory"):
                if docker.get(key):
                    resources[key] = docker[key]
            if resources:
                service["x-resource-hints"] = resources
            services[ticket["ticket_id"]] = service
        return yaml.safe_dump(
            {
                "version": "3.9",
                "name": f"lmr-agent-plan-{plan_id}",
                "x-local-model-router": {
                    "draft_only": True,
                    "note": "Generated for review only. V1 does not execute Docker Compose.",
                },
                "services": services,
            },
            sort_keys=False,
        )

    def list_plans(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.*,
                       COUNT(t.ticket_id) AS ticket_count,
                       SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
                       SUM(CASE WHEN t.status IN ('blocked','failed') THEN 1 ELSE 0 END) AS attention_count
                FROM plans p
                LEFT JOIN tickets t ON t.plan_id = p.plan_id
                GROUP BY p.plan_id
                ORDER BY p.updated_at DESC
                """
            ).fetchall()
        return {"plans": [self._plan_row(row) for row in rows]}

    def get_plan(self, plan_id: str, *, include_work: bool = True) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
            if row is None:
                raise OrchestratorError(f"plan '{plan_id}' not found", "plan_not_found", 404)
            tickets = conn.execute(
                "SELECT * FROM tickets WHERE plan_id = ? ORDER BY created_at ASC, ticket_id ASC",
                (plan_id,),
            ).fetchall()
        plan = self._plan_row(row)
        result: dict[str, Any] = {
            "ok": True,
            "plan": plan,
            "tickets": [self._ticket_row(ticket) for ticket in tickets],
        }
        if include_work:
            result["work"] = self._read_text(plan.get("work_path"))
        wake_path = plan.get("wake_path")
        if wake_path and Path(wake_path).is_file():
            result["wake"] = _json_loads(Path(wake_path).read_text(encoding="utf-8"), {})
        return result

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        row = self._ticket_lookup(ticket_id)
        ticket = self._ticket_row(row)
        return {
            "ok": True,
            "ticket": ticket,
            "task": self._read_text(ticket.get("task_path")),
            "dox_chain": self._read_text(ticket.get("dox_chain_path")),
        }

    def submit_ticket(self, ticket_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise OrchestratorError("request body must be a JSON object", "invalid_request_body", 400)
        status = str(body.get("status") or "").strip().lower()
        if status not in FINAL_TICKET_STATUSES:
            raise OrchestratorError(
                "submit status must be completed, blocked, or failed",
                "invalid_ticket_status",
                422,
            )
        dox_report = str(body.get("dox_report") or "").strip()
        dox_unchanged_reason = str(body.get("dox_unchanged_reason") or "").strip()
        if not dox_report and not dox_unchanged_reason:
            raise OrchestratorError(
                "submit requires dox_report or dox_unchanged_reason",
                "dox_response_required",
                422,
            )

        row = self._ticket_lookup(ticket_id)
        ticket = self._ticket_row(row)
        summary = str(body.get("summary") or "").strip()
        artifacts = body.get("artifacts") if isinstance(body.get("artifacts"), list) else []
        handoff_to = str(body.get("handoff_to") or "").strip() or None
        marker_name = {"completed": "DONE.json", "blocked": "BLOCKED.json", "failed": "FAILED.json"}[status]
        marker_path = Path(ticket["workspace_path"]) / marker_name
        if dox_report:
            Path(ticket["dox_report_path"]).write_text(dox_report + "\n", encoding="utf-8")
        marker_payload = {
            "ticket_id": ticket_id,
            "plan_id": ticket["plan_id"],
            "status": status,
            "summary": summary,
            "artifacts": artifacts,
            "dox_report_path": ticket["dox_report_path"],
            "dox_unchanged_reason": dox_unchanged_reason,
            "handoff_to": handoff_to,
            "created_at": _now_iso(),
        }
        marker_path.write_text(_json_dumps(marker_payload) + "\n", encoding="utf-8")

        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tickets
                SET status = ?, summary = ?, dox_unchanged_reason = ?,
                    handoff_to = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (status, summary, dox_unchanged_reason or None, handoff_to, now, ticket_id),
            )
            conn.execute(
                """
                INSERT INTO ticket_events (plan_id, ticket_id, event_type, payload_json, created_at)
                VALUES (?, ?, 'ticket_submitted', ?, ?)
                """,
                (ticket["plan_id"], ticket_id, _json_dumps(marker_payload), now),
            )

        self._refresh_ready_tickets(ticket["plan_id"])
        self._recompute_plan_status(ticket["plan_id"])
        detail = self.get_plan(ticket["plan_id"], include_work=False)
        detail["ticket"] = self._ticket_row(self._ticket_lookup(ticket_id))
        return detail

    def _refresh_ready_tickets(self, plan_id: str) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tickets WHERE plan_id = ?", (plan_id,)).fetchall()
            completed = {row["ticket_id"] for row in rows if row["status"] == "completed"}
            now = _now_iso()
            for row in rows:
                if row["status"] != "pending":
                    continue
                deps = _json_loads(row["dependencies_json"], [])
                if all(dep in completed for dep in deps):
                    conn.execute(
                        "UPDATE tickets SET status = 'ready', updated_at = ? WHERE ticket_id = ?",
                        (now, row["ticket_id"]),
                    )

    def _recompute_plan_status(self, plan_id: str) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tickets WHERE plan_id = ?", (plan_id,)).fetchall()
            required = [row for row in rows if bool(row["required"])]
            blocked = [row for row in required if row["status"] in {"blocked", "failed"}]
            all_required_done = bool(required) and all(row["status"] == "completed" for row in required)
            status = "open"
            wake: Optional[dict[str, Any]] = None
            if blocked:
                row = blocked[0]
                status = "needs_attention"
                reason = "required_ticket_failed" if row["status"] == "failed" else "required_ticket_blocked"
                wake = {
                    "plan_id": plan_id,
                    "reason": reason,
                    "ticket_id": row["ticket_id"],
                    "handoff_to": row["handoff_to"],
                    "created_at": _now_iso(),
                }
            elif all_required_done:
                status = "ready_for_planner"
                wake = {
                    "plan_id": plan_id,
                    "reason": "required_tickets_completed",
                    "handoff_to": "planner",
                    "created_at": _now_iso(),
                }

            wake_path = None
            if wake is not None:
                plan_row = conn.execute("SELECT workspace_path FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
                if plan_row is not None:
                    path = Path(plan_row["workspace_path"]) / "WAKE.json"
                    path.write_text(_json_dumps(wake) + "\n", encoding="utf-8")
                    wake_path = str(path)

            conn.execute(
                """
                UPDATE plans
                SET status = ?, wake_path = COALESCE(?, wake_path), updated_at = ?
                WHERE plan_id = ?
                """,
                (status, wake_path, _now_iso(), plan_id),
            )

    def _ticket_lookup(self, ticket_id: str) -> sqlite3.Row:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise OrchestratorError(f"ticket '{ticket_id}' not found", "ticket_not_found", 404)
        return row

    @staticmethod
    def _read_text(path: object) -> str:
        try:
            return Path(str(path)).read_text(encoding="utf-8")
        except Exception:
            return ""

    @staticmethod
    def _plan_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["planner"] = _json_loads(item.pop("planner_json", "{}"), {})
        for key in ("ticket_count", "completed_count", "attention_count"):
            if key in item:
                item[key] = int(item.get(key) or 0)
        return item

    @staticmethod
    def _ticket_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["required"] = bool(item.get("required"))
        item["dependencies"] = _json_loads(item.pop("dependencies_json", "[]"), [])
        item["target_paths"] = _json_loads(item.pop("target_paths_json", "[]"), [])
        item["capability_bundles"] = _json_loads(item.pop("capability_bundles_json", "[]"), [])
        item["resolved_capabilities"] = _json_loads(item.pop("resolved_capabilities_json", "{}"), {})
        item["docker"] = _json_loads(item.pop("docker_json", "{}"), {})
        return item
