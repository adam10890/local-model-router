---
name: imperium-work-pages
description: Plan multi-step work in Imperium and coordinate Pi workers through shared, leased tickets.
version: 0.1.0
author: Imperium
metadata:
  hermes:
    tags: [imperium, planning, multi-agent, work-pages]
    category: autonomous-ai-agents
    requires_toolsets: [terminal]
---

# Imperium Work Pages

Use this skill when a request has multiple deliverables or benefits from
independent workers. Keep ordinary conversation in Hermes.

## Roles

- Hermes owns the user relationship, goal, plan, dependencies, and final summary.
- Pi owns one claimed ticket at a time.
- Imperium owns routing and the shared SQLite/workspace state.
- Never copy personal memory into a worker prompt. Send only the brief,
  acceptance criteria, constraints, and explicit context references.

## Setup

Set `IMPERIUM_BASE_URL` to the router origin; it defaults to
`http://127.0.0.1:9000`. If the router uses bearer authentication, set
`IMPERIUM_API_KEY` in the environment. Never write the key into a plan file.

## Procedure

1. For real multi-step work, create a JSON plan with one ticket per executable
   step. Dependencies use ticket IDs.
2. Create the page:

   ```text
   python scripts/work_pages.py create plan.json
   ```

3. Give each Pi worker only its ticket ID and a stable worker ID. The Pi
   extension reads and claims the ticket.
4. Inspect progress without replanning completed work:

   ```text
   python scripts/work_pages.py get <plan_id>
   python scripts/work_pages.py ticket <ticket_id>
   ```

5. Replan only when a required ticket is blocked or failed. When all required
   tickets complete, read the page and summarize the results for the user.

## Plan payload

```json
{
  "plan_id": "page-auth-api",
  "conversation_id": "hermes-session-id",
  "goal": "Add authenticated task APIs",
  "planner": {"agent_id": "hermes", "model": "hermes/plan"},
  "work_document": "# Goal\n\nAdd authenticated task APIs.\n",
  "tickets": [
    {
      "id": "S1",
      "title": "Define API shape",
      "agent_role": "pi/work.research",
      "model_hint": "pi/work.research",
      "prompt": "Define the endpoint shape and acceptance criteria.",
      "target_paths": ["docs/"],
      "capability_bundles": ["research"]
    },
    {
      "id": "S2",
      "title": "Implement API",
      "agent_role": "pi/work.code",
      "model_hint": "pi/work.code",
      "dependencies": ["S1"],
      "prompt": "Implement the approved API shape. Run focused tests.",
      "target_paths": ["local_model_router/", "tests/"],
      "capability_bundles": ["code"]
    }
  ]
}
```

## Guardrails

- Hermes creates plans but does not claim worker tickets.
- A worker may update only a ticket it owns.
- Treat `409 ticket_claimed` as normal contention; choose another ready ticket.
- A completion must include an honest DOX report or unchanged reason.
- Do not put secrets, full personal memory, or unrelated chat history in tickets.
- The surface is an authenticated compatibility pilot and returns a
  `Deprecation` header; it does not launch workers by itself.

## Verification

`get <plan_id>` must show dependency-free tickets as `ready`. After a Pi worker
claims a ticket it must show `running`, `claimed_by`, `lease_until`, and an
incremented `attempt`.
