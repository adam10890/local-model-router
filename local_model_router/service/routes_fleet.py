"""Health, fleet telemetry/control, cookbook, routing catalog, agents, swarm, and A2A routes."""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Awaitable, Callable, Dict

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from local_model_router.a2a.card import agent_card, skill_ids
from local_model_router.execution import InventorySnapshot, PlanRequest, build_plan
from local_model_router.helpers import budget_engine
from local_model_router.routing.aliases import public_aliases
from local_model_router.routing.catalog import apply_evaluation_hints, build_slot_candidates
from .agent_library import (
    AGENT_INPUT_MAX_BYTES,
    AgentRunFailed,
    AgentRunTimeout,
    AgentRunnerUnavailable,
    run_agent,
)
from .context import (
    SERVICE_LOGGER,
    _SERVICE_NAME,
    _VERSION,
    RouterContext,
    _authorized,
    _dict_or_empty,
    _openai_error,
    resolve_app_hook,
)
from .fleet_control import FleetControlError
from .fleet_manager import (
    AgentIdentity,
    context_windows_from_slots,
    fleet_config_from_env,
    identity_from_headers,
    slots_model_snapshot,
)
from .models_listing import list_models
from .routing_intent import RoutingIntentRequest
from .swarm_gate import SwarmError, SwarmPolicyError, SwarmTerminalSpec


async def health(ctx: RouterContext, request: Request) -> JSONResponse:
    client_host = request.client.host if request.client else ""
    is_loopback = client_host in {"127.0.0.1", "::1", "localhost", "testclient"}
    payload: Dict[str, Any] = {
        "status": "ok",
        "service": _SERVICE_NAME,
        "version": _VERSION,
    }
    if (not ctx.api_key and is_loopback) or (bool(ctx.api_key) and _authorized(request, ctx.api_key)):
        payload["config_path"] = ctx.observer.config_path
    return JSONResponse(payload)


async def slots(ctx: RouterContext, request: Request) -> JSONResponse:
    return JSONResponse(ctx.observer.get_slots())


async def config_preview(ctx: RouterContext, request: Request) -> JSONResponse:
    return JSONResponse(ctx.observer.get_config_preview())


async def routing_preview(ctx: RouterContext, request: Request) -> JSONResponse:
    role = request.query_params.get("role", "chat")
    result = await ctx.observer.get_routing_preview(role)
    return JSONResponse(result)


async def health_slots(ctx: RouterContext, request: Request) -> JSONResponse:
    results = await ctx.observer.get_slots_health()
    return JSONResponse(results)


async def fleet_status(ctx: RouterContext, request: Request) -> JSONResponse:
    slots = ctx.observer.get_slots()
    snapshot = slots_model_snapshot(slots)
    context_windows = context_windows_from_slots(slots)
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    if snapshot_json != ctx.last_observer_snapshot["json"]:
        ctx.last_observer_snapshot["json"] = snapshot_json
        await asyncio.to_thread(ctx.store.record_model_snapshot, "observer_slots", snapshot)
    agents, request_summary, (vram, compute) = await asyncio.gather(
        asyncio.to_thread(ctx.store.list_agents),
        asyncio.to_thread(ctx.store.request_summary),
        ctx.compute_status(),
    )
    managed_slots = await ctx.fleet_control.status() if ctx.control_enabled and ctx.supports_start_stop else {}
    runtime_fields = (
        "running",
        "healthy",
        "failure_code",
        "exit_code",
        "restart_count",
        "uptime_s",
    )
    for slot in slots:
        managed = managed_slots.get(str(slot.get("id") or ""))
        if managed:
            slot["runtime"] = {field: managed.get(field) for field in runtime_fields}
    return JSONResponse(
        {
            "ok": True,
            "service": "a0-fleet-manager",
            "version": _VERSION,
            "config": fleet_config_from_env(),
            "queue": ctx.queue.snapshot(),
            "queues": {
                "local": {"mode": "bounded", **ctx.queue.snapshot()},
                "local:scribe": {"mode": "bounded", **ctx.admission_queues["local:scribe"].snapshot()},
                **{
                    f"upstream:{upstream.name}": (
                        {"mode": "bounded", **ctx.admission_queues[f"upstream:{upstream.name}"].snapshot()}
                        if f"upstream:{upstream.name}" in ctx.admission_queues
                        else {
                            "mode": "delegated",
                            "active": None,
                            "queued": None,
                            "max_active": None,
                            "max_queue": None,
                        }
                    )
                    for upstream in ctx.upstreams
                },
            },
            "agents": {
                "count": len(agents),
                "items": agents,
            },
            "requests": request_summary,
            "vram": vram,
            "compute": compute,
            "slots": slots,
            "model_residency": snapshot,
            "context_windows": context_windows,
            "docker_socket_enabled": ctx.control_enabled and ctx.fleet_backend == "docker",
            "fleet_control": {
                "enabled": ctx.control_enabled,
                "backend": ctx.fleet_backend,
                "supports_start_stop": ctx.supports_start_stop,
            },
        }
    )


def control_gated(
    ctx: RouterContext,
    handler: Callable[[Request], Awaitable[Response]],
) -> Callable[[Request], Awaitable[Response]]:
    async def wrapper(request: Request) -> Response:
        if not ctx.control_enabled:
            return _openai_error(
                "fleet control is disabled on this router; "
                "set A0_LMM_ROUTER_ENABLE_FLEET_CONTROL=1 and restart to enable slot start/stop",
                "fleet_control_disabled",
                403,
            )
        if ctx.fleet_backend == "remote":
            return _openai_error(
                "fleet backend is externally managed; start or stop its model servers outside Imperium",
                "remote_backend_unmanaged",
                409,
            )
        return await handler(request)

    return wrapper


async def _run_control(ctx: RouterContext, request: Request, action: str, *, reason: str | None = None) -> Response:
    slot_id = request.path_params.get("slot_id", "")
    try:
        if action == "start":
            if ctx.hermes_watcher is not None and slot_id == ctx.hermes_watcher.cfg.slot_id:
                ctx.hermes_watcher.reset_retry_budget()
            payload = await ctx.fleet_control.start_slot(slot_id)
        elif action == "stop":
            payload = await ctx.fleet_control.stop_slot(slot_id, reason=reason)
        elif action == "start_all":
            if ctx.hermes_watcher is not None:
                ctx.hermes_watcher.reset_retry_budget()
            payload = await ctx.fleet_control.start_all()
        else:
            payload = await ctx.fleet_control.stop_all()
    except FleetControlError as exc:
        return _openai_error(exc.message, exc.code, exc.status_code, error_type="server_error")
    except Exception as exc:  # pragma: no cover - defensive
        SERVICE_LOGGER.error("Fleet control action=%s failed (%s)", action, type(exc).__name__)
        return _openai_error(
            "fleet control action failed",
            "fleet_control_failed",
            502,
            error_type="server_error",
        )
    return JSONResponse(payload)


async def cookbook(ctx: RouterContext, request: Request) -> JSONResponse:
    import yaml

    from local_model_router.cookbook.engine import build_report

    try:
        with open(ctx.observer.config_path, encoding="utf-8") as fh:
            fleet_conf = yaml.safe_load(fh) or {}
    except Exception:
        fleet_conf = {}

    models_dir = (
        str(ctx.setup_engine._read_json(ctx.setup_engine.settings_path).get("models_dir") or "").strip()
        or os.environ.get("LLAMA_MODELS_DIR", "").strip()
        or str((fleet_conf.get("global") or {}).get("models_dir", "") or "").strip()
        or str(ctx.setup_engine.models_dir)
    )
    if not models_dir or not os.path.isdir(models_dir):
        return JSONResponse(
            {
                "ok": False,
                "error": "models_dir_not_configured",
                "detail": (
                    "Point the cookbook at your GGUF folder: set LLAMA_MODELS_DIR "
                    "in .env or global.models_dir in conf/llama_cpp_servers.yaml."
                ),
                "models_dir": models_dir or None,
            }
        )

    now = time.time()
    force = request.query_params.get("refresh") == "1"
    if (
        not force
        and ctx.cookbook_cache["report"] is not None
        and ctx.cookbook_cache["key"] == models_dir
        and now - ctx.cookbook_cache["at"] < 60
    ):
        return JSONResponse(ctx.cookbook_cache["report"])

    report = await asyncio.to_thread(
        build_report,
        models_dir,
        fleet_conf.get("hardware") or {},
        fleet_conf.get("context_policy") or {},
    )
    report["ok"] = True
    ctx.cookbook_cache.update({"key": models_dir, "at": now, "report": report})
    return JSONResponse(report)


async def fleet_slot_start(ctx: RouterContext, request: Request) -> Response:
    return await _run_control(ctx, request, "start")


async def fleet_slot_stop(ctx: RouterContext, request: Request) -> Response:
    reason = None
    if "application/json" in request.headers.get("content-type", ""):
        try:
            body = await request.json()
        except Exception:
            return _openai_error(
                "request body is not valid JSON",
                "invalid_json",
                400,
            )
        if not isinstance(body, dict):
            return _openai_error(
                "request body must be a JSON object",
                "invalid_request_body",
                400,
            )
        raw = body.get("reason")
        if raw is not None and not isinstance(raw, str):
            return _openai_error(
                "reason must be a string",
                "invalid_request_body",
                400,
            )
        reason = raw or None
    return await _run_control(ctx, request, "stop", reason=reason)


async def fleet_start_all(ctx: RouterContext, request: Request) -> Response:
    return await _run_control(ctx, request, "start_all")


async def fleet_stop_all(ctx: RouterContext, request: Request) -> Response:
    return await _run_control(ctx, request, "stop_all")


async def fleet_prepare_shutdown(ctx: RouterContext, request: Request) -> Response:
    watcher_task = ctx.watcher_task_ref.get("task")
    if watcher_task is not None and not watcher_task.done():
        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
    ctx.watcher_task_ref["task"] = None
    if ctx.hermes_watcher is not None:
        await ctx.hermes_watcher.stop()
    if not ctx.control_enabled or not ctx.supports_start_stop:
        return JSONResponse(
            {"ok": True, "action": "prepare_shutdown", "results": {}}
        )
    return await _run_control(ctx, request, "stop_all")


async def fleet_agents(ctx: RouterContext, request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "agents": ctx.store.list_agents()})


async def fleet_agents_register(ctx: RouterContext, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_request_body"}, status_code=400)

    try:
        agent = identity_from_headers(
            {
                "x-agent-id": str(body.get("agent_id") or "anonymous").strip() or "anonymous",
                "x-agent-type": str(body.get("agent_type") or "custom").strip() or "custom",
                "x-priority": str(body.get("priority") or "normal").strip().lower(),
            }
        )
    except ValueError as exc:
        return JSONResponse({"error": "invalid_priority", "detail": str(exc)}, status_code=422)

    registered = ctx.store.register_agent(agent, metadata=_dict_or_empty(body.get("metadata")))
    return JSONResponse({"ok": True, "agent": registered})


async def backends(ctx: RouterContext, request: Request) -> JSONResponse:
    fleet_entry = {
        "name": "local_fleet",
        "type": "llama_cpp_fleet",
        "enabled": True,
        "serves_inference": True,
        "slots": ctx.observer.get_slots(),
    }
    return JSONResponse({
        "backends": [fleet_entry] + [upstream.describe() for upstream in ctx.upstreams],
    })


async def compute_budget_endpoint(ctx: RouterContext, request: Request) -> JSONResponse:
    return JSONResponse(budget_engine.compute_budget(ctx.upstreams))


async def apps_list(ctx: RouterContext, request: Request) -> JSONResponse:
    return JSONResponse({"apps": ctx.app_profiles.list_profiles()})


def routing_catalog_models(ctx: RouterContext) -> list[Dict[str, Any]]:
    slots_with_health: list[Dict[str, Any]] = []
    for slot in apply_evaluation_hints(
        ctx.observer.get_slots(), ctx.store.latest_model_snapshot("model_evaluation")
    ):
        row = dict(slot)
        # /routing/models is read-only and fast; avoid live probes here.
        row.setdefault("health", row.get("health") or "unknown")
        slots_with_health.append(row)
    models: list[Dict[str, Any]] = []
    for alias, role in sorted(public_aliases().items()):
        normalized_role = str(role or "chat")
        models.append({
            "id": f"alias:{alias}",
            "model_id": alias,
            "source": "alias",
            "role": normalized_role,
            "backend_type": "router",
            "slot_id": None,
            "upstream_name": None,
            "context_size": 0,
            "health": "available",
            "capabilities": {
                "auto_route": alias == "auto",
                "tools": normalized_role in {"chat", "utility", "scribe", "task-dependent"},
                "vision": False,
                "json_mode": normalized_role not in {"embed", "embedding"},
            },
            "hints": {"latency_ms": None, "quality": 0.0, "resource_cost": 0.0},
            "metadata": {"kind": "alias", "maps_to_role": role},
        })
    models.extend(candidate.public_dict() for candidate in build_slot_candidates(slots_with_health))
    for upstream in ctx.upstreams:
        entry = upstream.describe()
        models.append({
            "id": upstream.name,
            "model_id": upstream.name,
            "source": "upstream",
            "role": "chat",
            "backend_type": upstream.type,
            "slot_id": None,
            "upstream_name": upstream.name,
            "context_size": 0,
            "health": "unknown" if upstream.serves_inference else "disabled",
            "capabilities": {
                "tools": "tools" in upstream.capabilities,
                "vision": "vision" in upstream.capabilities,
                "json_mode": "json_mode" in upstream.capabilities or upstream.serves_inference,
            },
            "hints": {"latency_ms": None, "quality": 0.5, "resource_cost": 0.0},
            "metadata": {k: v for k, v in entry.items() if k != "base_url"},
        })
    return models


async def routing_models(ctx: RouterContext, request: Request) -> JSONResponse:
    return JSONResponse({"models": routing_catalog_models(ctx)})


async def routing_evaluations(ctx: RouterContext, request: Request) -> JSONResponse:
    snapshot = ctx.store.latest_model_snapshot("model_evaluation")
    return JSONResponse(snapshot or {
        "source": "model_evaluation",
        "created_at": None,
        "payload": {"schema_version": 1, "models": []},
    })


async def routing_model_card(ctx: RouterContext, request: Request) -> JSONResponse:
    requested = str(request.path_params.get("model_id") or "").strip()
    for model in routing_catalog_models(ctx):
        if requested in {str(model.get("id")), str(model.get("model_id"))}:
            return JSONResponse({"model": model})
    return JSONResponse({"error": "model_not_found", "model_id": requested}, status_code=404)


async def routing_analytics(ctx: RouterContext, request: Request) -> JSONResponse:
    try:
        limit = max(1, min(1000, int(request.query_params.get("limit", "50"))))
    except ValueError:
        limit = 50
    return JSONResponse(await asyncio.to_thread(ctx.store.routing_analytics, limit=limit))


async def agents_list(ctx: RouterContext, request: Request) -> JSONResponse:
    return JSONResponse({"agents": ctx.agent_catalog.public_list()})


async def agent_run(ctx: RouterContext, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_agent_input"}, status_code=400)

    agent_id = str(request.path_params.get("agent_id") or "").strip()
    definition = ctx.agent_catalog.get(agent_id)
    if definition is None:
        return JSONResponse({"error": "agent_not_found", "agent_id": agent_id}, status_code=404)

    user_input = body.get("input")
    if not isinstance(user_input, str) or not user_input.strip():
        return JSONResponse({"error": "invalid_agent_input"}, status_code=400)
    try:
        input_size = len(user_input.encode("utf-8"))
    except UnicodeEncodeError:
        return JSONResponse({"error": "invalid_agent_input"}, status_code=400)
    if input_size > AGENT_INPUT_MAX_BYTES:
        return JSONResponse({"error": "input_too_large"}, status_code=413)

    try:
        output = await resolve_app_hook("run_agent", run_agent)(definition, user_input)
    except AgentRunnerUnavailable:
        return JSONResponse({"error": "agent_runner_unavailable"}, status_code=503)
    except AgentRunTimeout:
        return JSONResponse({"error": "agent_timeout"}, status_code=504)
    except AgentRunFailed:
        return JSONResponse({"error": "agent_model_error"}, status_code=502)
    return JSONResponse({"agent_id": definition.id, "output": output})


async def swarm_terminals_spawn(ctx: RouterContext, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_request_body"}, status_code=400)
    try:
        spec = SwarmTerminalSpec.model_validate(body)
    except ValidationError as exc:
        return JSONResponse({"error": "invalid_swarm_request", "detail": exc.errors(include_url=False)}, status_code=422)
    try:
        terminal = await ctx.swarm_registry.spawn(spec)
    except SwarmError as exc:
        return JSONResponse({"error": exc.code, "detail": exc.message}, status_code=exc.status_code)
    return JSONResponse(terminal.public_dict(), status_code=201)


async def swarm_terminal_release(ctx: RouterContext, request: Request) -> JSONResponse:
    terminal_id = str(request.path_params.get("terminal_id") or "").strip()
    if not terminal_id:
        return JSONResponse({"error": "terminal_id_required"}, status_code=400)
    released = await ctx.swarm_registry.release(terminal_id)
    if not released:
        return JSONResponse({"error": "terminal_not_found"}, status_code=404)
    return JSONResponse({"ok": True, "terminal_id": terminal_id})


async def swarm_terminal_heartbeat(ctx: RouterContext, request: Request) -> JSONResponse:
    terminal_id = str(request.path_params.get("terminal_id") or "").strip()
    if not await ctx.swarm_registry.heartbeat(terminal_id):
        # Reaped or released: 404 is the sub's signal to stop using the slot.
        return JSONResponse({"error": "terminal_not_found"}, status_code=404)
    return JSONResponse({"ok": True, "terminal_id": terminal_id})


async def swarm_capacity_view(ctx: RouterContext, request: Request) -> JSONResponse:
    return JSONResponse(ctx.swarm_registry.snapshot())


async def execution_plan(ctx: RouterContext, request: Request) -> JSONResponse:
    if not ctx.execution_catalog.settings.enabled:
        return JSONResponse(
            {
                "error": "execution_disabled",
                "detail": "execution planning is disabled; set execution.enabled: true in conf/execution.yaml",
            },
            status_code=403,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_request_body"}, status_code=400)
    try:
        spec = PlanRequest.model_validate(body)
    except ValidationError as exc:
        return JSONResponse(
            {"error": "invalid_execution_request", "detail": exc.errors(include_url=False)},
            status_code=422,
        )
    slots = ctx.observer.get_slots()
    vram, _compute = await ctx.compute_status()
    models = [
        model_id
        for slot in slots
        if (model_id := str(slot.get("model_id") or "").strip())
    ]
    inventory = InventorySnapshot(
        free_vram_mb=float(vram.get("available_gb") or 0) * 1024,
        loaded_models=models,
    )
    plan = build_plan(spec, ctx.execution_catalog, inventory)
    return JSONResponse(plan.model_dump(mode="json"))


async def well_known_agent_card(request: Request) -> JSONResponse:
    base_url = str(request.base_url).rstrip("/")
    return JSONResponse(agent_card(base_url))


async def a2a_skills(ctx: RouterContext, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_request_body"}, status_code=400)

    skill = str(body.get("skill") or "").strip()
    skill_input = _dict_or_empty(body.get("input"))
    if skill not in skill_ids():
        return JSONResponse(
            {"error": "unknown_skill", "known_skills": sorted(skill_ids())},
            status_code=404,
        )

    if skill == "route_llm_task":
        try:
            intent = RoutingIntentRequest.model_validate({
                "agent_id": str(skill_input.get("agent_id") or "a2a_client"),
                "agent_type": str(skill_input.get("agent_type") or "custom"),
                "role": skill_input.get("role"),
                "task_type": str(skill_input.get("task_type") or "chat"),
                "privacy_mode": str(skill_input.get("privacy_mode") or "unknown"),
                "local_only": bool(skill_input.get("local_only", False)),
                "requires_long_context": bool(skill_input.get("requires_long_context", False)),
                "requires_tools": bool(skill_input.get("requires_tools", False)),
                "estimated_tokens": skill_input.get("estimated_tokens"),
                "preferred_slot": skill_input.get("preferred_slot"),
            })
        except ValidationError as exc:
            import json as _json

            return JSONResponse(
                {"error": "validation_error", "detail": _json.loads(exc.json())},
                status_code=422,
            )
        decision = await ctx.intent_handler.handle(intent)
        return JSONResponse({"skill": skill, "result": decision.model_dump()})

    if skill == "check_backend_health":
        slots_health = await ctx.observer.get_slots_health()
        return JSONResponse({
            "skill": skill,
            "result": {
                "fleet_slots": slots_health,
                "upstreams": [upstream.describe() for upstream in ctx.upstreams],
            },
        })

    # list_models
    listing = await list_models(ctx.observer, fetch=ctx.models_fetch, http_client=ctx.http_client)
    return JSONResponse({"skill": skill, "result": listing})


async def routing_request(ctx: RouterContext, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)

    try:
        intent = RoutingIntentRequest.model_validate(body)
    except ValidationError as exc:
        import json as _json
        return JSONResponse({"error": "validation_error", "detail": _json.loads(exc.json())}, status_code=422)

    result = await ctx.intent_handler.handle(intent)
    return JSONResponse(result.model_dump())


async def openapi_spec(_ctx: RouterContext, request: Request) -> JSONResponse:
    from .openapi_spec import build_openapi_spec

    return JSONResponse(build_openapi_spec(request.app.routes, _VERSION))



def build_fleet_routes(ctx: RouterContext) -> list[Route]:
    return [
        Route("/health", ctx.bind(health)),
        Route("/openapi.json", ctx.protected(ctx.bind(openapi_spec))),
        Route("/slots", ctx.protected(ctx.bind(slots))),
        Route("/config/preview", ctx.protected(ctx.bind(config_preview))),
        Route("/routing/preview", ctx.protected(ctx.bind(routing_preview))),
        Route("/health/slots", ctx.protected(ctx.bind(health_slots))),
        Route("/fleet/status", ctx.protected(ctx.bind(fleet_status))),
        Route("/fleet/agents", ctx.protected(ctx.bind(fleet_agents))),
        Route("/fleet/agents/register", ctx.protected(ctx.bind(fleet_agents_register)), methods=["POST"]),
        Route("/fleet/start", ctx.protected(control_gated(ctx, ctx.bind(fleet_start_all))), methods=["POST"]),
        Route("/fleet/stop", ctx.protected(control_gated(ctx, ctx.bind(fleet_stop_all))), methods=["POST"]),
        Route("/fleet/prepare-shutdown", ctx.protected(ctx.bind(fleet_prepare_shutdown)), methods=["POST"]),
        Route(
            "/fleet/slots/{slot_id}/start",
            ctx.protected(control_gated(ctx, ctx.bind(fleet_slot_start))),
            methods=["POST"],
        ),
        Route(
            "/fleet/slots/{slot_id}/stop",
            ctx.protected(control_gated(ctx, ctx.bind(fleet_slot_stop))),
            methods=["POST"],
        ),
        Route("/routing/request", ctx.protected(ctx.bind(routing_request)), methods=["POST"]),
        Route("/routing/models", ctx.protected(ctx.bind(routing_models))),
        Route("/routing/evaluations", ctx.protected(ctx.bind(routing_evaluations))),
        Route("/routing/models/{model_id:path}", ctx.protected(ctx.bind(routing_model_card))),
        Route("/routing/analytics", ctx.protected(ctx.bind(routing_analytics))),
        Route("/backends", ctx.protected(ctx.bind(backends))),
        Route("/compute/budget", ctx.protected(ctx.bind(compute_budget_endpoint))),
        Route("/apps", ctx.protected(ctx.bind(apps_list))),
        Route("/agents", ctx.protected(ctx.bind(agents_list))),
        Route("/agents/{agent_id}/runs", ctx.protected(ctx.bind(agent_run)), methods=["POST"]),
        Route("/v1/swarm/terminals", ctx.protected(ctx.bind(swarm_terminals_spawn)), methods=["POST"]),
        Route("/v1/swarm/terminals/{terminal_id}/release", ctx.protected(ctx.bind(swarm_terminal_release)), methods=["POST"]),
        Route("/v1/swarm/terminals/{terminal_id}/heartbeat", ctx.protected(ctx.bind(swarm_terminal_heartbeat)), methods=["POST"]),
        Route("/v1/swarm/capacity", ctx.protected(ctx.bind(swarm_capacity_view)), methods=["GET"]),
        Route("/v1/execution/plan", ctx.protected(ctx.bind(execution_plan)), methods=["POST"]),
        Route("/cookbook", ctx.protected(ctx.bind(cookbook))),
        Route("/.well-known/agent-card.json", well_known_agent_card),
        Route("/a2a", ctx.protected(ctx.bind(a2a_skills)), methods=["POST"]),
    ]
