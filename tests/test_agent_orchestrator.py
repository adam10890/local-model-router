from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path

from starlette.testclient import TestClient


_FLEET_CONFIG = """\
active_slots:
  - id: utility
    port: 8088
    host: localhost
    role: utility
    enabled: true
    model_id: utility-model
global:
  backend: remote
"""


def _make_client(tmp_path, monkeypatch):
    from local_model_router.service.agent_orchestrator import AgentOrchestrator
    from local_model_router.service.app import create_app

    fleet_cfg = tmp_path / "llama_cpp_servers.yaml"
    fleet_cfg.write_text(_FLEET_CONFIG, encoding="utf-8")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "AGENTS.md").write_text("# Root DOX\n\nRoot rules.\n", encoding="utf-8")
    service_dir = repo_root / "local_model_router" / "service"
    service_dir.mkdir(parents=True)
    (service_dir / "AGENTS.md").write_text("# Service DOX\n\nService rules.\n", encoding="utf-8")

    bundles = tmp_path / "agent_orchestrator.yaml"
    bundles.write_text(
        textwrap.dedent(
            """
            bundles:
              code:
                mcp_servers: ["filesystem"]
                plugins: ["GitHub"]
                tools: ["shell", "pytest"]
              docs:
                mcp_servers: []
                plugins: []
                tools: ["markdown"]
            """
        ),
        encoding="utf-8",
    )

    orchestrator = AgentOrchestrator(
        db_path=str(tmp_path / "orchestrator.sqlite3"),
        workspace_root=str(tmp_path / "agent-work"),
        repo_root=str(repo_root),
        bundles_path=str(bundles),
    )
    monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", "")
    app = create_app(str(fleet_cfg), orchestrator=orchestrator)
    return TestClient(app), orchestrator, repo_root


def _plan_payload() -> dict:
    return {
        "goal": "Implement a local multi-agent feature",
        "conversation_id": "conv-123",
        "planner": {"agent_id": "planner-main", "model": "deep-local"},
        "work_document": "# Work\n\nBuild the thing.",
        "tickets": [
            {
                "id": "ticket-code",
                "title": "Code worker",
                "prompt": "Implement the API surface.",
                "agent_role": "coder",
                "target_paths": ["local_model_router/service/app.py"],
                "capability_bundles": ["code"],
                "docker": {"image": "agent-runner:planned", "cpus": "2", "memory": "4g"},
            },
            {
                "id": "ticket-docs",
                "title": "Docs worker",
                "prompt": "Prepare documentation updates.",
                "agent_role": "scribe",
                "dependencies": ["ticket-code"],
                "required": True,
                "target_paths": ["README.md"],
                "capability_bundles": ["docs"],
            },
        ],
    }


def test_create_plan_writes_sqlite_rows_workspace_files_and_dox_chain(tmp_path, monkeypatch):
    client, orchestrator, repo_root = _make_client(tmp_path, monkeypatch)

    resp = client.post("/orchestrator/plans", json=_plan_payload())

    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"]["status"] == "open"
    assert body["tickets"][0]["status"] == "ready"
    assert body["tickets"][1]["status"] == "pending"

    plan_dir = Path(body["plan"]["workspace_path"])
    assert (plan_dir / "work.md").read_text(encoding="utf-8") == "# Work\n\nBuild the thing."
    assert (plan_dir / "plan.json").is_file()
    compose = (plan_dir / "compose.plan.yaml").read_text(encoding="utf-8")
    assert "draft_only: true" in compose
    assert "ticket-code" in compose

    ticket_dir = plan_dir / "tickets" / "ticket-code"
    assert (ticket_dir / "task.md").read_text(encoding="utf-8").startswith("# Code worker")
    ticket_json = json.loads((ticket_dir / "ticket.json").read_text(encoding="utf-8"))
    assert ticket_json["model_hint"] == "WeiboAI/VibeThinker-3B"
    assert ticket_json["prompt_path"].endswith("task.md")
    assert "Implement the API surface." not in json.dumps(ticket_json)

    capabilities = json.loads((ticket_dir / "capabilities.json").read_text(encoding="utf-8"))
    assert capabilities["requested_bundles"] == ["code"]
    assert capabilities["resolved"]["tools"] == ["shell", "pytest"]

    dox = (ticket_dir / "dox_chain.md").read_text(encoding="utf-8")
    assert str(repo_root / "AGENTS.md") in dox
    assert str(repo_root / "local_model_router" / "service" / "AGENTS.md") in dox

    with sqlite3.connect(orchestrator.db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"plans", "tickets", "ticket_events"}.issubset(tables)


def test_submit_requires_dox_response_and_writes_done_blocked_and_wake_files(tmp_path, monkeypatch):
    client, _orchestrator, _repo_root = _make_client(tmp_path, monkeypatch)
    plan = client.post("/orchestrator/plans", json=_plan_payload()).json()["plan"]

    missing_dox = client.post(
        "/orchestrator/tickets/ticket-code/submit",
        json={"status": "completed", "summary": "API done."},
    )
    assert missing_dox.status_code == 422
    assert missing_dox.json()["error"] == "dox_response_required"

    done = client.post(
        "/orchestrator/tickets/ticket-code/submit",
        json={
            "status": "completed",
            "summary": "API done.",
            "artifacts": [{"path": "artifacts/api-notes.md", "kind": "notes"}],
            "dox_report": "Read root and service DOX; propose no direct edits.",
        },
    )
    assert done.status_code == 200
    assert done.json()["ticket"]["status"] == "completed"
    assert done.json()["plan"]["status"] == "open"
    plan_dir = Path(plan["workspace_path"])
    assert (plan_dir / "tickets" / "ticket-code" / "DONE.json").is_file()

    blocked = client.post(
        "/orchestrator/tickets/ticket-docs/submit",
        json={
            "status": "blocked",
            "summary": "Need final wording from planner.",
            "dox_unchanged_reason": "No files changed by this sub-agent.",
            "handoff_to": "planner-main",
        },
    )
    assert blocked.status_code == 200
    assert blocked.json()["plan"]["status"] == "needs_attention"
    assert (plan_dir / "tickets" / "ticket-docs" / "BLOCKED.json").is_file()
    wake = json.loads((plan_dir / "WAKE.json").read_text(encoding="utf-8"))
    assert wake["reason"] == "required_ticket_blocked"
    assert wake["handoff_to"] == "planner-main"


def test_all_required_tickets_completed_marks_plan_ready_for_planner(tmp_path, monkeypatch):
    client, _orchestrator, _repo_root = _make_client(tmp_path, monkeypatch)
    created = client.post("/orchestrator/plans", json=_plan_payload()).json()
    plan_dir = Path(created["plan"]["workspace_path"])

    for ticket_id in ("ticket-code", "ticket-docs"):
        resp = client.post(
            f"/orchestrator/tickets/{ticket_id}/submit",
            json={
                "status": "completed",
                "summary": f"{ticket_id} done.",
                "dox_report": "DOX read; no direct edits.",
            },
        )
        assert resp.status_code == 200

    detail = client.get(f"/orchestrator/plans/{created['plan']['plan_id']}").json()
    assert detail["plan"]["status"] == "ready_for_planner"
    assert detail["wake"]["reason"] == "required_tickets_completed"
    assert (plan_dir / "WAKE.json").is_file()


def test_orchestrator_list_and_ticket_detail_are_summary_safe(tmp_path, monkeypatch):
    client, _orchestrator, _repo_root = _make_client(tmp_path, monkeypatch)
    created = client.post("/orchestrator/plans", json=_plan_payload()).json()

    listing = client.get("/orchestrator/plans").json()
    assert listing["plans"][0]["goal"] == "Implement a local multi-agent feature"
    assert "work_document" not in listing["plans"][0]
    assert "Implement the API surface." not in str(listing)

    ticket = client.get("/orchestrator/tickets/ticket-code").json()
    assert ticket["ticket"]["ticket_id"] == "ticket-code"
    assert ticket["task"].endswith("Implement the API surface.\n")
    assert ticket["ticket"]["artifact_path"].endswith("artifacts")
    assert created["plan"]["plan_id"] == ticket["ticket"]["plan_id"]


def test_dashboard_exposes_observe_only_orchestration_tab():
    from local_model_router.dashboard import dashboard_html

    html = dashboard_html()
    assert "Orchestration" in html
    assert "/orchestrator/plans" in html
    assert "orchestration.ticket" in html
    assert "Create plan" not in html
