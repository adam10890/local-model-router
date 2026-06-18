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

- **Done:** per-harness `roles` map in `conf/apps.yaml`; `app_profiles.apply`
  resolves a role to the harness's pinned model before the global failover
  chain (admin pin beats `allowed_models`); `/apps` surfaces it; dashboard
  **Harnesses** tab renders the matrix.
- **Done — planner serving:** VibeThinker is served by **Docker Model Runner**
  (`docker model pull hf.co/mradermacher/VibeThinker-3B-GGUF:Q8_0`), wired as
  the `dmr` upstream in `conf/upstreams.yaml`; the A0 `planner` pin points at
  `dmr/huggingface.co/mradermacher/vibethinker-3b-gguf:Q8_0`. Verified
  end-to-end (`model: planner` → router → DMR → VibeThinker → 42).
  > Caveat: DMR is a peer compute manager — its VRAM is **not** under the
  > router's admission control. Fine while `backend: remote`; revisit when
  > fork #1 lands (migrate the planner into the native managed fleet if it must
  > coexist in the 24 GB budget).
- **Open (fork #1) — runtime compute manager:** the router owns the GPU at
  runtime, not just request routing. The pieces exist — fit engine
  (`cookbook/engine.py`), fleet control, VRAM budget + `max_concurrent` limits
  in `conf/llama_cpp_servers.yaml`. Missing is the admission loop that
  loads/evicts models on demand within the 24 GB budget. Flip
  `global.backend: docker` + wire that loop and the router self-manages compute
  as load shifts (chat ↔ planner ↔ sub-agent bursts).

> Follow-up: VibeThinker emits `<think>…</think>` reasoning in content; add a
> reasoning-format split when the orchestrator starts consuming planner output.
