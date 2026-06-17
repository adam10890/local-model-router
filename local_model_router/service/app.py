"""
Starlette app factory for the lmm-router read-only observer service.

Usage:
    from service.app import create_app
    app = create_app("/path/to/llama_cpp_servers.yaml")

Or run via python -m local_model_router.service (see __main__.py).
"""
from __future__ import annotations

import hmac
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

import aiohttp
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from local_model_router.a2a.card import agent_card, skill_ids
from local_model_router.apps.profiles import AppProfiles
from local_model_router.routing.aliases import public_aliases, resolve_alias
from local_model_router.routing.catalog import (
    build_slot_candidates,
    required_capabilities_from_chat_body,
)
from local_model_router.upstreams.registry import (
    UpstreamConfig,
    load_upstreams,
    match_upstream_model,
)

from .fleet_control import (
    FleetControlError,
    FleetControlHandler,
    configured_backend,
    fleet_control_enabled,
)
from .agent_orchestrator import AgentOrchestrator, OrchestratorError
from .fleet_manager import (
    AgentIdentity,
    FleetQueue,
    FleetStore,
    QueueFull,
    context_windows_from_slots,
    fleet_config_from_env,
    identity_from_headers,
    slots_model_snapshot,
    vram_unknown_summary,
)
from .models_listing import FetchFn, _default_fetch as _models_default_fetch, list_models
from .observer import ObserverBackend
from .prompt_cache import (
    InMemoryPromptCache,
    cache_key as prompt_cache_key,
    is_cacheable as prompt_is_cacheable,
    prompt_cache_enabled,
)
from .routing_intent import RoutingIntentHandler, RoutingIntentRequest

from local_model_router import __version__ as _VERSION
_SERVICE_NAME = "lmm-router-observer"
_FORWARD_TIMEOUT_SECONDS = 120
_API_KEY_ENV = "A0_LMM_ROUTER_API_KEY"

_ROUTING_ONLY_KEYS = {
    "routing",
    "preferred_slot",
    "role",
    "agent_id",
    "agent_type",
    "app_id",
    "task_type",
    "privacy_mode",
    "local_only",
    "cloud_allowed",
    "requires_long_context",
    "requires_tools",
    "requires_vision",
    "requires_json_mode",
    "requires_code_execution",
    "routing_strategy",
    "latency_preference",
    "quality_preference",
    "cost_preference",
    "estimated_tokens",
    "input_classification",
}


def _openai_error(
    message: str,
    code: str,
    status_code: int,
    *,
    error_type: str = "invalid_request_error",
    param: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    body: Dict[str, Any] = {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status_code)


def _configured_api_key() -> str:
    return os.environ.get(_API_KEY_ENV, "").strip()


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


def _authorized(request: Request, api_key: str) -> bool:
    if not api_key:
        return True
    return hmac.compare_digest(_bearer_token(request), api_key)


def _unauthorized_response(request: Request) -> JSONResponse:
    if request.url.path.startswith("/v1/"):
        return _openai_error(
            "missing or invalid bearer token",
            "unauthorized",
            401,
            error_type="authentication_error",
        )
    return JSONResponse(
        {"error": "unauthorized", "detail": "missing or invalid bearer token"},
        status_code=401,
    )


def _dict_or_empty(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pick_routing_value(
    body: Dict[str, Any],
    routing: Dict[str, Any],
    metadata: Dict[str, Any],
    key: str,
    default: Any = None,
) -> Any:
    if key in routing:
        return routing[key]
    if key in body:
        return body[key]
    if key in metadata:
        return metadata[key]
    return default


def _role_from_chat_body(body: Dict[str, Any], routing: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    explicit_role = _pick_routing_value(body, routing, metadata, "role")
    if explicit_role:
        return str(explicit_role)

    task_type = str(_pick_routing_value(body, routing, metadata, "task_type", "") or "").lower()
    resolution = resolve_alias(body.get("model"), task_type=task_type or "chat")
    if resolution.recognized and resolution.role:
        return resolution.role

    model = str(body.get("model", "") or "").lower()
    if "utility" in model or model in {"util", "coding", "debugging"}:
        return "utility"
    if task_type in {
        "background_worker",
        "coding",
        "debugging",
        "planning",
        "private_data_processing",
        "research",
        "sub_agent_task",
        "tool_calling",
    }:
        return "utility"
    if task_type == "embedding":
        return "embed"
    return "chat"


def _intent_from_chat_body(
    body: Dict[str, Any],
    agent: Optional[AgentIdentity] = None,
    *,
    app_id: Optional[str] = None,
    requested_model: Optional[str] = None,
) -> RoutingIntentRequest:
    routing = _dict_or_empty(body.get("routing"))
    metadata = _dict_or_empty(body.get("metadata"))
    role = _role_from_chat_body(body, routing, metadata)
    needs = required_capabilities_from_chat_body(body, role=role)

    payload = {
        "agent_id": _pick_routing_value(
            body, routing, metadata, "agent_id", body.get("user") or "openai_compatible_client"
        ),
        "agent_type": _pick_routing_value(body, routing, metadata, "agent_type", "custom"),
        "role": role,
        "task_type": _pick_routing_value(body, routing, metadata, "task_type", "chat"),
        "privacy_mode": _pick_routing_value(body, routing, metadata, "privacy_mode", "unknown"),
        "local_only": _pick_routing_value(body, routing, metadata, "local_only", False),
        "cloud_allowed": _pick_routing_value(body, routing, metadata, "cloud_allowed", True),
        "requires_long_context": _pick_routing_value(
            body, routing, metadata, "requires_long_context", needs.requires_long_context
        ),
        "requires_tools": _pick_routing_value(body, routing, metadata, "requires_tools", needs.requires_tools),
        "requires_vision": _pick_routing_value(body, routing, metadata, "requires_vision", needs.requires_vision),
        "requires_json_mode": _pick_routing_value(
            body, routing, metadata, "requires_json_mode", needs.requires_json_mode
        ),
        "requires_code_execution": _pick_routing_value(
            body, routing, metadata, "requires_code_execution", False
        ),
        "latency_preference": _pick_routing_value(body, routing, metadata, "latency_preference", "normal"),
        "quality_preference": _pick_routing_value(body, routing, metadata, "quality_preference", "normal"),
        "cost_preference": _pick_routing_value(body, routing, metadata, "cost_preference", "normal"),
        "routing_strategy": _pick_routing_value(body, routing, metadata, "routing_strategy", needs.strategy),
        "estimated_tokens": _pick_routing_value(body, routing, metadata, "estimated_tokens", needs.estimated_tokens),
        "preferred_slot": _pick_routing_value(body, routing, metadata, "preferred_slot", needs.preferred_slot),
        "requested_model": requested_model or body.get("model"),
        "app_id": app_id,
        "input_classification": _pick_routing_value(body, routing, metadata, "input_classification"),
        "metadata": metadata,
    }
    if agent is not None:
        payload["agent_id"] = agent.agent_id
        payload["agent_type"] = agent.agent_type
    return RoutingIntentRequest.model_validate(payload)


def _forward_payload(body: Dict[str, Any], selected_model: Optional[str], *, stream: bool) -> Dict[str, Any]:
    payload = {k: v for k, v in body.items() if k not in _ROUTING_ONLY_KEYS}
    payload["stream"] = stream
    payload["model"] = selected_model or payload.get("model") or "local"
    return payload


def _app_id_from(headers: Any, body: Dict[str, Any], agent: Optional[AgentIdentity]) -> str:
    """Client app id: X-App-Id header > body app_id > agent type."""
    header = str(headers.get("x-app-id", "") or "").strip()
    if header:
        return header
    body_app = body.get("app_id")
    if isinstance(body_app, str) and body_app.strip():
        return body_app.strip()
    return getattr(agent, "agent_type", "") or ""


def _forward_model_for(body: Dict[str, Any], task_type: str, decision_model: Optional[str]) -> Optional[str]:
    """Model id to send upstream.

    Recognized aliases (``auto``, ``fast``, ``coder``, …) forward the routing
    decision's model. Unrecognized names are explicit model requests — they
    pass through untouched so a Router Mode fleet (or upstream backend) can
    serve the exact id the client asked for.
    """
    resolution = resolve_alias(body.get("model"), task_type=task_type or "chat")
    if resolution.recognized:
        return decision_model
    return resolution.requested or decision_model


def _estimate_prompt_tokens(body: Dict[str, Any]) -> int:
    """Small mixed-language token estimate for OpenAI-compatible payloads."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    chars = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        chars += len(text)
    return max(0, chars // 3)


def _context_for_slot(
    slots: list[Dict[str, Any]],
    slot_id: Optional[str],
    selected_model: Optional[str] = None,
) -> Dict[str, Any]:
    windows = context_windows_from_slots(slots)
    for item in windows:
        if item.get("slot_id") == slot_id and selected_model and item.get("alias") == selected_model:
            return item
    for item in windows:
        if item.get("slot_id") == slot_id:
            return item
    return {}


def _context_headers(context: Dict[str, Any], prompt_tokens: int, selected_model: Optional[str]) -> Dict[str, str]:
    hard_ctx = context.get("hard_ctx")
    effective_ctx = context.get("effective_ctx")
    try:
        occupancy = float(prompt_tokens) / float(hard_ctx) if hard_ctx else None
    except (TypeError, ValueError, ZeroDivisionError):
        occupancy = None
    return {
        "x-selected-model": selected_model or "",
        "x-hard-ctx": str(hard_ctx) if hard_ctx else "unknown",
        "x-effective-ctx": str(effective_ctx) if effective_ctx else "unknown",
        "x-context-occupancy": f"{occupancy:.4f}" if occupancy is not None else "unknown",
        "x-a0-router-hard-ctx": str(hard_ctx) if hard_ctx else "unknown",
        "x-a0-router-effective-ctx": str(effective_ctx) if effective_ctx else "unknown",
        "x-a0-router-context-occupancy": f"{occupancy:.4f}" if occupancy is not None else "unknown",
    }


async def _stream_upstream_response(resp: aiohttp.ClientResponse, session: aiohttp.ClientSession):
    try:
        async for chunk in resp.content.iter_chunked(8192):
            if chunk:
                yield chunk
    except aiohttp.ClientError as exc:
        import json as _json

        payload = {
            "error": {
                "message": f"upstream llama.cpp stream failed: {exc}",
                "type": "server_error",
                "param": None,
                "code": "upstream_stream_error",
            }
        }
        yield f"data: {_json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")
    finally:
        resp.release()
        await session.close()


def create_app(
    config_path: Optional[str] = None,
    *,
    fleet_store: Optional[FleetStore] = None,
    fleet_queue: Optional[FleetQueue] = None,
    models_fetch: Optional[FetchFn] = None,
    upstreams_path: Optional[str] = None,
    apps_path: Optional[str] = None,
    orchestrator: Optional[AgentOrchestrator] = None,
) -> Starlette:
    """Return a configured Starlette app.  Safe to call multiple times (no side-effects)."""
    observer = ObserverBackend(config_path)
    intent_handler = RoutingIntentHandler(observer)
    api_key = _configured_api_key()
    store = fleet_store or FleetStore()
    queue = fleet_queue or FleetQueue()
    prompt_cache = InMemoryPromptCache() if prompt_cache_enabled() else None

    conf_dir = Path(observer.config_path).resolve().parent
    upstreams = load_upstreams(upstreams_path or conf_dir / "upstreams.yaml")
    app_profiles = AppProfiles.load(apps_path or conf_dir / "apps.yaml")
    agent_orchestrator = orchestrator or AgentOrchestrator(
        repo_root=str(Path(__file__).resolve().parents[2]),
        bundles_path=str(conf_dir / "agent_orchestrator.yaml"),
    )

    control_enabled = fleet_control_enabled()
    fleet_control = FleetControlHandler(observer.config_path)
    fleet_backend = configured_backend(observer.config_path)

    def protected(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        async def wrapper(request: Request) -> Response:
            if not _authorized(request, api_key):
                return _unauthorized_response(request)
            return await handler(request)

        return wrapper

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "service": _SERVICE_NAME,
            "version": _VERSION,
            "config_path": observer.config_path,
        })

    async def slots(request: Request) -> JSONResponse:
        return JSONResponse(observer.get_slots())

    async def config_preview(request: Request) -> JSONResponse:
        return JSONResponse(observer.get_config_preview())

    async def routing_preview(request: Request) -> JSONResponse:
        role = request.query_params.get("role", "chat")
        result = await observer.get_routing_preview(role)
        return JSONResponse(result)

    async def health_slots(request: Request) -> JSONResponse:
        results = await observer.get_slots_health()
        return JSONResponse(results)

    async def fleet_status(request: Request) -> JSONResponse:
        slots = observer.get_slots()
        snapshot = slots_model_snapshot(slots)
        context_windows = context_windows_from_slots(slots)
        store.record_model_snapshot("observer_slots", snapshot)
        agents = store.list_agents()
        return JSONResponse(
            {
                "ok": True,
                "service": "a0-fleet-manager",
                "version": _VERSION,
                "config": fleet_config_from_env(),
                "queue": queue.snapshot(),
                "agents": {
                    "count": len(agents),
                    "items": agents,
                },
                "requests": store.request_summary(),
                "vram": vram_unknown_summary(),
                "slots": slots,
                "model_residency": snapshot,
                "context_windows": context_windows,
                "docker_socket_enabled": control_enabled and fleet_backend == "docker",
                "fleet_control": {
                    "enabled": control_enabled,
                    "backend": fleet_backend,
                },
            }
        )

    def control_gated(
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def wrapper(request: Request) -> Response:
            if not control_enabled:
                return _openai_error(
                    "fleet control is disabled on this router; "
                    "set A0_LMM_ROUTER_ENABLE_FLEET_CONTROL=1 and restart to enable slot start/stop",
                    "fleet_control_disabled",
                    403,
                )
            return await handler(request)

        return wrapper

    async def _run_control(request: Request, action: str) -> Response:
        slot_id = request.path_params.get("slot_id", "")
        try:
            if action == "start":
                payload = await fleet_control.start_slot(slot_id)
            elif action == "stop":
                payload = await fleet_control.stop_slot(slot_id)
            elif action == "start_all":
                payload = await fleet_control.start_all()
            else:
                payload = await fleet_control.stop_all()
        except FleetControlError as exc:
            return _openai_error(exc.message, exc.code, exc.status_code, error_type="server_error")
        except Exception as exc:  # pragma: no cover - defensive
            return _openai_error(
                f"fleet control action failed: {exc}",
                "fleet_control_failed",
                502,
                error_type="server_error",
            )
        return JSONResponse(payload)

    _cookbook_cache: Dict[str, Any] = {"key": None, "at": 0.0, "report": None}

    async def cookbook(request: Request) -> JSONResponse:
        import yaml

        from local_model_router.cookbook.engine import build_report

        try:
            with open(observer.config_path, encoding="utf-8") as fh:
                fleet_conf = yaml.safe_load(fh) or {}
        except Exception:
            fleet_conf = {}

        models_dir = (
            os.environ.get("LLAMA_MODELS_DIR", "").strip()
            or str((fleet_conf.get("global") or {}).get("models_dir", "") or "").strip()
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
            and _cookbook_cache["report"] is not None
            and _cookbook_cache["key"] == models_dir
            and now - _cookbook_cache["at"] < 60
        ):
            return JSONResponse(_cookbook_cache["report"])

        report = build_report(
            models_dir,
            fleet_conf.get("hardware") or {},
            fleet_conf.get("context_policy") or {},
        )
        report["ok"] = True
        _cookbook_cache.update({"key": models_dir, "at": now, "report": report})
        return JSONResponse(report)

    async def fleet_slot_start(request: Request) -> Response:
        return await _run_control(request, "start")

    async def fleet_slot_stop(request: Request) -> Response:
        return await _run_control(request, "stop")

    async def fleet_start_all(request: Request) -> Response:
        return await _run_control(request, "start_all")

    async def fleet_stop_all(request: Request) -> Response:
        return await _run_control(request, "stop_all")

    async def fleet_agents(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "agents": store.list_agents()})

    async def fleet_agents_register(request: Request) -> JSONResponse:
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

        registered = store.register_agent(agent, metadata=_dict_or_empty(body.get("metadata")))
        return JSONResponse({"ok": True, "agent": registered})

    async def v1_models(request: Request) -> JSONResponse:
        listing = await list_models(observer, fetch=models_fetch)
        fetch_fn = models_fetch or _models_default_fetch
        existing_ids = {row["id"] for row in listing["data"]}
        for upstream in upstreams:
            if not upstream.serves_inference:
                continue
            rows = await fetch_fn(upstream.base_url) or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                bare_id = str(row.get("id") or "").strip()
                if not bare_id:
                    continue
                namespaced = f"{upstream.name}/{bare_id}"
                if namespaced in existing_ids:
                    continue
                existing_ids.add(namespaced)
                capabilities = {
                    "tools": "tools" in upstream.capabilities,
                    "vision": "vision" in upstream.capabilities,
                    "json_mode": "json_mode" in upstream.capabilities or upstream.serves_inference,
                }
                listing["data"].append({
                    "id": namespaced,
                    "object": "model",
                    "created": row.get("created") or 0,
                    "owned_by": upstream.name,
                    "capabilities": capabilities,
                    "source": "upstream",
                    "meta": {
                        "kind": "upstream_model",
                        "upstream": upstream.name,
                        "capabilities": capabilities,
                    },
                })
        return JSONResponse(listing)

    async def backends(request: Request) -> JSONResponse:
        fleet_entry = {
            "name": "local_fleet",
            "type": "llama_cpp_fleet",
            "enabled": True,
            "serves_inference": True,
            "slots": observer.get_slots(),
        }
        return JSONResponse({
            "backends": [fleet_entry] + [upstream.describe() for upstream in upstreams],
        })

    async def apps_list(request: Request) -> JSONResponse:
        return JSONResponse({"apps": app_profiles.list_profiles()})

    def routing_catalog_models() -> list[Dict[str, Any]]:
        slots_with_health: list[Dict[str, Any]] = []
        for slot in observer.get_slots():
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
        for upstream in upstreams:
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

    async def routing_models(request: Request) -> JSONResponse:
        return JSONResponse({"models": routing_catalog_models()})

    async def routing_model_card(request: Request) -> JSONResponse:
        requested = str(request.path_params.get("model_id") or "").strip()
        for model in routing_catalog_models():
            if requested in {str(model.get("id")), str(model.get("model_id"))}:
                return JSONResponse({"model": model})
        return JSONResponse({"error": "model_not_found", "model_id": requested}, status_code=404)

    async def routing_analytics(request: Request) -> JSONResponse:
        try:
            limit = max(1, min(1000, int(request.query_params.get("limit", "50"))))
        except ValueError:
            limit = 50
        return JSONResponse(store.routing_analytics(limit=limit))

    def _orchestrator_error(exc: OrchestratorError) -> JSONResponse:
        return JSONResponse({"error": exc.code, "detail": exc.message}, status_code=exc.status_code)

    async def orchestrator_create_plan(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
        try:
            return JSONResponse(agent_orchestrator.create_plan(body))
        except OrchestratorError as exc:
            return _orchestrator_error(exc)

    async def orchestrator_list_plans(request: Request) -> JSONResponse:
        return JSONResponse(agent_orchestrator.list_plans())

    async def orchestrator_plan_detail(request: Request) -> JSONResponse:
        plan_id = str(request.path_params.get("plan_id") or "")
        try:
            return JSONResponse(agent_orchestrator.get_plan(plan_id))
        except OrchestratorError as exc:
            return _orchestrator_error(exc)

    async def orchestrator_ticket_detail(request: Request) -> JSONResponse:
        ticket_id = str(request.path_params.get("ticket_id") or "")
        try:
            return JSONResponse(agent_orchestrator.get_ticket(ticket_id))
        except OrchestratorError as exc:
            return _orchestrator_error(exc)

    async def orchestrator_ticket_submit(request: Request) -> JSONResponse:
        ticket_id = str(request.path_params.get("ticket_id") or "")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
        try:
            return JSONResponse(agent_orchestrator.submit_ticket(ticket_id, body))
        except OrchestratorError as exc:
            return _orchestrator_error(exc)

    async def forward_to_upstream(
        upstream: UpstreamConfig, bare_model: str, body: Dict[str, Any]
    ) -> Response:
        wants_stream = body.get("stream") is True
        payload = _forward_payload(body, bare_model, stream=wants_stream)
        url = upstream.base_url + "/chat/completions"
        out_headers = {
            "x-a0-router-upstream": upstream.name,
            "x-a0-router-model": bare_model,
            "x-a0-router-requested-model": str(body.get("model") or ""),
            "x-a0-router-resolved-model": bare_model,
            "x-a0-router-strategy": "explicit_upstream",
            "x-a0-router-cache": "BYPASS",
        }
        try:
            if wants_stream:
                session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=_FORWARD_TIMEOUT_SECONDS)
                )
                try:
                    resp = await session.post(url, json=payload, headers=upstream.headers())
                except Exception:
                    await session.close()
                    raise
                if resp.status >= 400:
                    try:
                        upstream_json = await resp.json(content_type=None)
                    except Exception:
                        upstream_json = await resp.text()
                    resp.release()
                    await session.close()
                    return _openai_error(
                        f"upstream {upstream.name} stream request failed",
                        "upstream_error",
                        resp.status,
                        error_type="server_error",
                        extra={"upstream": upstream_json},
                    )
                return StreamingResponse(
                    _stream_upstream_response(resp, session),
                    status_code=resp.status,
                    media_type="text/event-stream",
                    headers={**out_headers, "cache-control": "no-cache", "x-accel-buffering": "no"},
                )

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=upstream.headers(),
                    timeout=aiohttp.ClientTimeout(total=_FORWARD_TIMEOUT_SECONDS),
                ) as resp:
                    try:
                        upstream_json = await resp.json(content_type=None)
                    except Exception:
                        upstream_text = await resp.text()
                        return _openai_error(
                            f"upstream {upstream.name} response was not valid JSON",
                            "upstream_invalid_json",
                            502,
                            error_type="server_error",
                            extra={"upstream_status": resp.status, "upstream_body": upstream_text[:1000]},
                        )
                    return JSONResponse(upstream_json, status_code=resp.status, headers=out_headers)
        except aiohttp.ClientError as exc:
            return _openai_error(
                f"could not reach upstream {upstream.name}: {exc}",
                "upstream_unreachable",
                502,
                error_type="server_error",
            )
        except TimeoutError:
            return _openai_error(
                f"upstream {upstream.name} timed out",
                "upstream_timeout",
                504,
                error_type="server_error",
            )

    async def dashboard_page(request: Request) -> HTMLResponse:
        from local_model_router.dashboard import dashboard_html

        return HTMLResponse(dashboard_html())

    async def well_known_agent_card(request: Request) -> JSONResponse:
        base_url = str(request.base_url).rstrip("/")
        return JSONResponse(agent_card(base_url))

    async def a2a_skills(request: Request) -> JSONResponse:
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
            decision = await intent_handler.handle(intent)
            return JSONResponse({"skill": skill, "result": decision.model_dump()})

        if skill == "check_backend_health":
            slots_health = await observer.get_slots_health()
            return JSONResponse({
                "skill": skill,
                "result": {
                    "fleet_slots": slots_health,
                    "upstreams": [upstream.describe() for upstream in upstreams],
                },
            })

        # list_models
        listing = await list_models(observer, fetch=models_fetch)
        return JSONResponse({"skill": skill, "result": listing})

    async def routing_request(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)

        try:
            intent = RoutingIntentRequest.model_validate(body)
        except ValidationError as exc:
            import json as _json
            return JSONResponse({"error": "validation_error", "detail": _json.loads(exc.json())}, status_code=422)

        result = await intent_handler.handle(intent)
        return JSONResponse(result.model_dump())

    async def chat_completions(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return _openai_error("request body is not valid JSON", "invalid_json", 400)

        if not isinstance(body, dict):
            return _openai_error("request body must be a JSON object", "invalid_request_body", 400)

        if "messages" not in body:
            return _openai_error("missing required field: messages", "missing_messages", 400, param="messages")

        try:
            agent = identity_from_headers(request.headers, body)
            app_id = _app_id_from(request.headers, body, agent)
            requested_model = str(body.get("model") or "auto").strip() or "auto"
            effective_model, policy_error = app_profiles.apply(app_id, body.get("model"))
            if policy_error:
                return _openai_error(
                    f"model '{effective_model}' is not allowed for app '{app_id}'",
                    policy_error,
                    403,
                    param="model",
                    extra={"app_id": app_id},
                )
            body["model"] = effective_model
            intent = _intent_from_chat_body(
                body,
                agent=agent,
                app_id=app_id,
                requested_model=requested_model,
            )
        except ValidationError as exc:
            import json as _json
            return _openai_error(
                "routing metadata failed validation",
                "routing_validation_error",
                422,
                extra={"detail": _json.loads(exc.json())},
            )
        except ValueError as exc:
            return _openai_error(
                "invalid request priority; use low, normal, or high",
                "invalid_priority",
                422,
                param="X-Priority",
                extra={"detail": str(exc)},
            )

        upstream_match = match_upstream_model(effective_model, upstreams)
        if upstream_match is not None:
            return await forward_to_upstream(upstream_match[0], upstream_match[1], body)

        request_id = store.create_request(agent)
        started = time.monotonic()
        try:
            admission = await queue.acquire(agent.priority)
        except QueueFull as exc:
            store.update_request(
                request_id,
                status="rejected",
                error_code="queue_full",
                app_id=app_id,
                requested_model=requested_model,
                routing_strategy=intent.routing_strategy,
                cache_status="BYPASS",
            )
            store.record_queue_event(
                request_id=request_id,
                agent=agent,
                event_type="queue_full",
                queue_depth=exc.queue_depth,
                active_count=queue.snapshot()["active"],
            )
            return _openai_error(
                "local fleet queue is full",
                "queue_full",
                429,
                error_type="server_error",
                extra={
                    "queue": {
                        "queue_depth": exc.queue_depth,
                        "max_queue": exc.max_queue,
                    }
                },
            )

        store.update_request(request_id, status="admitted", queued_ms=admission.queued_ms)
        store.record_queue_event(
            request_id=request_id,
            agent=agent,
            event_type="admitted",
            queue_depth=admission.queue_depth_at_admit,
            active_count=admission.active_at_admit,
        )

        def fleet_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
            current = queue.snapshot()
            headers = {
                "x-a0-agent-id": agent.agent_id,
                "x-a0-agent-type": agent.agent_type,
                "x-a0-fleet-request-id": request_id,
                "x-a0-fleet-queue-depth": str(current["queued"]),
                "x-a0-fleet-vram-available-gb": "unknown",
            }
            if extra:
                headers.update(extra)
            return headers

        async def finish(
            status: str,
            *,
            error_code: Optional[str] = None,
            slot_id: Optional[str] = None,
            model: Optional[str] = None,
            decision: Any = None,
            cache_status: Optional[str] = None,
            usage: Optional[Dict[str, Any]] = None,
        ) -> None:
            duration_ms = int((time.monotonic() - started) * 1000)
            usage = usage or {}
            store.update_request(
                request_id,
                status=status,
                slot_id=slot_id,
                model=model,
                queued_ms=admission.queued_ms,
                duration_ms=duration_ms,
                error_code=error_code,
                app_id=app_id,
                requested_model=requested_model,
                resolved_model=model or getattr(decision, "selected_model", None),
                routing_strategy=getattr(decision, "routing_strategy", intent.routing_strategy),
                selected_source=getattr(decision, "selected_source", None),
                fallback_used=getattr(decision, "fallback_used", False),
                reason_codes=getattr(decision, "reason_codes", None),
                cache_status=cache_status,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
            )
            await queue.release()

        try:
            decision = await intent_handler.handle(intent)
        except Exception as exc:
            await finish("failed", error_code="routing_error")
            return _openai_error(
                f"routing decision failed: {type(exc).__name__}: {exc}",
                "routing_error",
                500,
                error_type="server_error",
            )
        decision_body = decision.model_dump()
        if decision.no_slot_available or not decision.selected_url:
            await finish("failed", error_code="no_slot_available", decision=decision, cache_status="BYPASS")
            return _openai_error(
                "no healthy local llama.cpp slot is available for this request",
                "no_slot_available",
                503,
                error_type="server_error",
                extra={"routing": decision_body},
            )

        url = decision.selected_url.rstrip("/") + "/chat/completions"
        wants_stream = body.get("stream") is True
        forward_model = _forward_model_for(body, intent.task_type, decision.selected_model)
        payload = _forward_payload(body, forward_model, stream=wants_stream)
        selected_context = _context_for_slot(
            observer.get_slots(),
            decision.selected_slot_id,
            decision.selected_model,
        )
        context_extra_headers = _context_headers(
            selected_context,
            _estimate_prompt_tokens(body),
            decision.selected_model,
        )

        def router_response_headers(cache_status: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
            headers = {
                "x-a0-router-slot-id": decision.selected_slot_id or "",
                "x-a0-router-backend": decision.selected_backend_type or "",
                "x-a0-router-requested-model": requested_model,
                "x-a0-router-resolved-model": decision.selected_model or "",
                "x-a0-router-strategy": decision.routing_strategy,
                "x-a0-router-cache": cache_status,
            }
            if decision.selected_model:
                headers["x-a0-router-model"] = decision.selected_model
            headers.update(context_extra_headers)
            if extra:
                headers.update(extra)
            return fleet_headers(headers)

        cache_status = "BYPASS"
        cache_lookup_key: Optional[str] = None
        if prompt_cache is not None and not wants_stream and prompt_is_cacheable(payload):
            cache_lookup_key = prompt_cache_key(payload, resolved_model=forward_model or decision.selected_model or "")
            cached = prompt_cache.get(cache_lookup_key)
            if cached is not None:
                cache_status = "HIT"
                await finish(
                    "completed",
                    slot_id=decision.selected_slot_id,
                    model=decision.selected_model,
                    decision=decision,
                    cache_status=cache_status,
                    usage=_dict_or_empty(cached.get("usage")),
                )
                return JSONResponse(
                    cached,
                    status_code=200,
                    headers=router_response_headers(cache_status),
                )
            cache_status = "MISS"

        try:
            if wants_stream:
                session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_FORWARD_TIMEOUT_SECONDS))
                try:
                    resp = await session.post(url, json=payload, headers={"Content-Type": "application/json"})
                except Exception:
                    await session.close()
                    raise

                headers = router_response_headers(
                    "BYPASS",
                    {
                    "cache-control": "no-cache",
                    "x-accel-buffering": "no",
                    },
                )

                if resp.status >= 400:
                    try:
                        upstream_json = await resp.json(content_type=None)
                    except Exception:
                        upstream_json = await resp.text()
                    resp.release()
                    await session.close()
                    await finish(
                        "failed",
                        error_code="upstream_error",
                        slot_id=decision.selected_slot_id,
                        model=decision.selected_model,
                        decision=decision,
                        cache_status="BYPASS",
                    )

                    message = "upstream llama.cpp stream request failed"
                    if isinstance(upstream_json, dict):
                        upstream_error = upstream_json.get("error")
                        if isinstance(upstream_error, dict) and upstream_error.get("message"):
                            message = str(upstream_error["message"])
                        elif isinstance(upstream_error, str):
                            message = upstream_error
                    return _openai_error(
                        message,
                        "upstream_error",
                        resp.status,
                        error_type="server_error",
                        extra={"upstream": upstream_json, "routing": decision_body},
                    )

                async def managed_stream():
                    try:
                        async for chunk in _stream_upstream_response(resp, session):
                            yield chunk
                    finally:
                        await finish(
                            "completed",
                            slot_id=decision.selected_slot_id,
                            model=decision.selected_model,
                            decision=decision,
                            cache_status="BYPASS",
                        )

                return StreamingResponse(
                    managed_stream(),
                    status_code=resp.status,
                    media_type="text/event-stream",
                    headers=headers,
                )

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=_FORWARD_TIMEOUT_SECONDS),
                ) as resp:
                    try:
                        upstream_json = await resp.json(content_type=None)
                    except Exception:
                        upstream_text = await resp.text()
                        await finish(
                            "failed",
                            error_code="upstream_invalid_json",
                            slot_id=decision.selected_slot_id,
                            model=decision.selected_model,
                            decision=decision,
                            cache_status=cache_status,
                        )
                        return _openai_error(
                            "upstream llama.cpp response was not valid JSON",
                            "upstream_invalid_json",
                            502,
                            error_type="server_error",
                            extra={"upstream_status": resp.status, "upstream_body": upstream_text[:1000]},
                        )

                    headers = router_response_headers(cache_status)

                    if resp.status >= 400:
                        message = "upstream llama.cpp request failed"
                        if isinstance(upstream_json, dict):
                            upstream_error = upstream_json.get("error")
                            if isinstance(upstream_error, dict) and upstream_error.get("message"):
                                message = str(upstream_error["message"])
                            elif isinstance(upstream_error, str):
                                message = upstream_error
                        await finish(
                            "failed",
                            error_code="upstream_error",
                            slot_id=decision.selected_slot_id,
                            model=decision.selected_model,
                            decision=decision,
                            cache_status=cache_status,
                            usage=_dict_or_empty(upstream_json.get("usage")),
                        )
                        return _openai_error(
                            message,
                            "upstream_error",
                            resp.status,
                            error_type="server_error",
                            extra={"upstream": upstream_json, "routing": decision_body},
                        )

                    await finish(
                        "completed",
                        slot_id=decision.selected_slot_id,
                        model=decision.selected_model,
                        decision=decision,
                        cache_status=cache_status,
                        usage=_dict_or_empty(upstream_json.get("usage")),
                    )
                    if cache_status == "MISS" and cache_lookup_key is not None and prompt_cache is not None:
                        prompt_cache.set(cache_lookup_key, upstream_json)
                    return JSONResponse(upstream_json, status_code=resp.status, headers=headers)
        except aiohttp.ClientError as exc:
            await finish(
                "failed",
                error_code="upstream_unreachable",
                slot_id=decision.selected_slot_id,
                model=decision.selected_model,
                decision=decision,
                cache_status=cache_status,
            )
            return _openai_error(
                f"could not reach selected llama.cpp slot: {exc}",
                "upstream_unreachable",
                502,
                error_type="server_error",
                extra={"routing": decision_body},
            )
        except TimeoutError:
            await finish(
                "failed",
                error_code="upstream_timeout",
                slot_id=decision.selected_slot_id,
                model=decision.selected_model,
                decision=decision,
                cache_status=cache_status,
            )
            return _openai_error(
                "selected llama.cpp slot timed out",
                "upstream_timeout",
                504,
                error_type="server_error",
                extra={"routing": decision_body},
            )

    routes = [
        Route("/health", health),
        Route("/slots", protected(slots)),
        Route("/config/preview", protected(config_preview)),
        Route("/routing/preview", protected(routing_preview)),
        Route("/health/slots", protected(health_slots)),
        Route("/fleet/status", protected(fleet_status)),
        Route("/fleet/agents", protected(fleet_agents)),
        Route("/fleet/agents/register", protected(fleet_agents_register), methods=["POST"]),
        Route("/fleet/start", protected(control_gated(fleet_start_all)), methods=["POST"]),
        Route("/fleet/stop", protected(control_gated(fleet_stop_all)), methods=["POST"]),
        Route("/fleet/slots/{slot_id}/start", protected(control_gated(fleet_slot_start)), methods=["POST"]),
        Route("/fleet/slots/{slot_id}/stop", protected(control_gated(fleet_slot_stop)), methods=["POST"]),
        Route("/routing/request", protected(routing_request), methods=["POST"]),
        Route("/routing/models", protected(routing_models)),
        Route("/routing/models/{model_id:path}", protected(routing_model_card)),
        Route("/routing/analytics", protected(routing_analytics)),
        Route("/orchestrator/plans", protected(orchestrator_create_plan), methods=["POST"]),
        Route("/orchestrator/plans", protected(orchestrator_list_plans)),
        Route("/orchestrator/plans/{plan_id}", protected(orchestrator_plan_detail)),
        Route("/orchestrator/tickets/{ticket_id}/submit", protected(orchestrator_ticket_submit), methods=["POST"]),
        Route("/orchestrator/tickets/{ticket_id}", protected(orchestrator_ticket_detail)),
        Route("/backends", protected(backends)),
        Route("/apps", protected(apps_list)),
        Route("/cookbook", protected(cookbook)),
        Route("/.well-known/agent-card.json", well_known_agent_card),
        Route("/a2a", protected(a2a_skills), methods=["POST"]),
        Route("/ui", dashboard_page),
        Route("/v1/models", protected(v1_models)),
        Route("/v1/chat/completions", protected(chat_completions), methods=["POST"]),
    ]

    return Starlette(routes=routes)
