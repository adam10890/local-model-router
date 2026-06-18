# Harness Consumption Matrix

Source of truth for **which local model serves which role for which harness**.
This is both the plan ("planning the models") and the data the dashboard's
Harnesses tab renders. The router is the serving layer beneath each harness;
this doc makes that layer visible.

A harness is any client (Agent Zero, Hermes, n8n, aider…) that talks to the
router over the OpenAI-compatible surface and identifies via `X-App-Id`.

---

## Agent Zero

Full consumer — uses every role. Foreground chat runs alongside the background
planner + workers.

| role         | model                | when                                                |
|--------------|----------------------|-----------------------------------------------------|
| `chat` (ego) | `gemma-4-12b`        | foreground — what Adam talks to                     |
| `planner`    | `vibethinker-3b`     | background — decomposes work, emits sub-agent tickets |
| `sub_agents` | per bundle (below)   | background — ticket runners                          |
| `scribe`     | `gemma-4-E4B`        | background — super-ego documentation                |
| `utility`    | `qwen3.5-9b`         | on demand                                           |
| `embedding`  | `nomic-embed-text`   | on demand                                           |

Sub-agent bundles (from `conf/agent_orchestrator.yaml`):

| bundle     | model                       |
|------------|-----------------------------|
| `code`     | `deepseek-coder-v2-lite`    |
| `research` | `mistral-nemo`              |
| `docs`     | `gemma-4-E4B` (shared w/ scribe) |
| `docker`   | `qwen3.5-9b` (shared w/ utility) |

## Hermes

Thin consumer. In Hermes, the model name is set to `LMM ROUTER`; the dashboard
shows what it actually resolves to locally.

| role   | Hermes sees  | local model     |
|--------|--------------|-----------------|
| `chat` | `LMM ROUTER` | `gemma-4-12b`   |

---

## Orchestration flow (planner → sub-agents)

`vibethinker-3b` is the planner. The router's `agent_orchestrator` already
hardcodes it: `DEFAULT_MODEL_HINT = "WeiboAI/VibeThinker-3B"`
(`local_model_router/service/agent_orchestrator.py`).

```
  A0 hook ──▶ plan (status: ready_for_planner)
                  │
        VibeThinker (planner, background)
                  │  emits
                  ▼
            tickets ── code / research / docs / docker
                  │  consumed by
                  ▼
        sub-agent instances ◀── fleet queue (bounded)

  gemma-4-12b (chat ego) runs concurrently, foreground.
```

The **hook system** is the trigger: an Agent Zero lifecycle hook posts a `plan`
to the orchestrator; VibeThinker plans it into tickets; workers run them. The
plan/ticket/instance state machine + SQLite already exist. V1 is observe-first
— the orchestrator does **not** launch containers yet (see fork #1 below).

## VRAM coexistence (RTX 4090 / 24 GB)

| set                                    | approx | fits |
|----------------------------------------|--------|------|
| gemma-4-12b + vibethinker + scribe-E4B | ~18 GB | yes  |
| gemma-4-26b-uncensored + vibethinker   | ~20 GB | yes  |
| gemma-4-26b + vibethinker + scribe     | ~26 GB | no   |

12b is the default ego because it lets all three background+foreground models
coexist. Swap to 26b only if you accept the scribe time-sharing the GPU.

---

## Wiring status

- **Now (this doc):** the plan + DOX. Nothing in the serving path reads it yet.
- **Next (`feature/harness-role-pins`):** extend `conf/apps.yaml` so each app
  carries a per-role model map, and make `app_profiles.apply` honor it before
  the global failover chain. Proposed schema:

  ```yaml
  agent_zero:
    display_name: "Agent Zero"
    roles: { chat: gemma-4-12b, planner: vibethinker-3b,
             utility: qwen3.5-9b, embedding: nomic-embed-text }
  hermes:
    display_name: "LMM ROUTER"
    roles: { chat: gemma-4-12b }
  ```
- **Later (`feature/dashboard-harnesses-tab`):** dashboard tab renders this
  matrix + live health, one card per app.
- **Open (fork #1) — runtime compute manager:** the router owns the GPU at
  runtime, not just request routing. The pieces exist — fit engine
  (`cookbook/engine.py`), fleet control, VRAM budget + `max_concurrent` limits
  in `conf/llama_cpp_servers.yaml`. Missing is the admission loop that
  loads/evicts models on demand within the 24 GB budget. Flip
  `global.backend: docker` + wire that loop and the router self-manages compute
  as load shifts (chat ↔ planner ↔ sub-agent bursts).

> Pending: `vibethinker-3b` GGUF download + preset entry (see session notes).
> Until then the planner role has no backing model on disk.
