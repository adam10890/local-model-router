"""Harness endpoints, pinned model passthrough, and dashboard HTTP shell."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import time
from typing import Any, Dict, Optional

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from local_model_router.harnesses import HarnessConfigError, setup_manifest
from .context import (
    _UI_SECURITY_HEADERS,
    RouterContext,
    _capabilities_for_pinned_model,
    _context_for_pinned_model,
    _openai_error,
    _pin_model_allowed,
)
from .routes_openai import handle_chat_completions


def harness_manifest(ctx: RouterContext, profile: Any) -> Dict[str, Any]:
    slots = ctx.observer.get_slots()
    activity = {
        connection_name: ctx.harness_activity[f"{profile.harness_id}/{connection_name}"]
        for connection_name in profile.connections
        if f"{profile.harness_id}/{connection_name}" in ctx.harness_activity
    }
    caps_by_connection = {
        connection.name: _capabilities_for_pinned_model(
            connection.model, slots, ctx.upstreams
        )
        for connection in profile.connections.values()
    }
    context_by_connection = {
        connection.name: context
        for connection in profile.connections.values()
        if (context := _context_for_pinned_model(connection.model, slots)) is not None
    }
    return setup_manifest(
        profile,
        auth_required=bool(ctx.api_key),
        verification_by_connection=activity,
        capabilities_by_connection=caps_by_connection,
        context_by_connection=context_by_connection,
    )


def resolve_harness_request(ctx: RouterContext, request: Request) -> tuple[Any, Any] | JSONResponse:
    harness_id = request.path_params.get("harness_id", "")
    connection_name = request.path_params.get("connection")
    try:
        profile = ctx.harness_profiles.get(harness_id)
    except (HarnessConfigError, KeyError):
        return JSONResponse(
            {"error": "unknown_harness", "harness_id": harness_id},
            status_code=404,
        )
    try:
        connection = ctx.harness_profiles.resolve(harness_id, connection_name)
    except (HarnessConfigError, KeyError):
        return JSONResponse(
            {
                "error": "unknown_harness_connection",
                "harness_id": harness_id,
                "connection": connection_name,
            },
            status_code=404,
        )
    return profile, connection


def mark_harness_activity(
    ctx: RouterContext,
    profile: Any,
    connection: Any,
    state: str,
    *,
    client_verified: bool = False,
) -> None:
    key = f"{profile.harness_id}/{connection.name}"
    now = datetime.now(timezone.utc).isoformat()
    prev = dict(ctx.harness_activity.get(key) or {})
    prev_state = str(prev.get("state") or "not_seen")
    effective_state = (
        prev_state
        if prev_state in {"verified", "client_verified"} and state in {"connected", "seen"}
        else state
    )
    record: Dict[str, Any] = {
        **prev,
        "state": effective_state,
        "last_seen": now,
    }
    if state in {"connected", "seen", "verified", "client_verified"}:
        record["reachable_at"] = now
    if state in {"verified", "client_verified"}:
        record["completion_verified_at"] = now
    if client_verified or state == "client_verified":
        record["client_verified"] = True
        record["client_verified_at"] = now
    ctx.harness_activity[key] = record


async def harnesses_list(ctx: RouterContext, request: Request) -> JSONResponse:
    return JSONResponse({
        "harnesses": [harness_manifest(ctx, profile) for profile in ctx.harness_profiles.list_profiles()],
        "source": ctx.harness_profiles.source,
        "config_writes_enabled": ctx.config_writes_enabled,
    })


def config_write_denial(ctx: RouterContext) -> Optional[JSONResponse]:
    if not ctx.config_writes_requested:
        return JSONResponse({"error": "config_writes_disabled"}, status_code=403)
    if not ctx.api_key:
        return JSONResponse({"error": "config_write_requires_api_key"}, status_code=403)
    return None


async def harness_create(ctx: RouterContext, request: Request) -> JSONResponse:
    denial = config_write_denial(ctx)
    if denial is not None:
        return denial
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid_request_body"}, status_code=400)
    try:
        ctx.harness_profiles, _backup = ctx.harness_profiles.upsert(payload)
        profile = ctx.harness_profiles.get(str(payload.get("harness_id") or ""))
    except HarnessConfigError as exc:
        return JSONResponse(
            {"error": "invalid_harness_config", "detail": str(exc)},
            status_code=422,
        )
    return JSONResponse(harness_manifest(ctx, profile), status_code=201)


async def harness_pin(ctx: RouterContext, request: Request) -> JSONResponse:
    denial = config_write_denial(ctx)
    if denial is not None:
        return denial
    harness_id = request.path_params.get("harness_id", "")
    connection_name = request.path_params.get("connection", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_request_body"}, status_code=400)
    model = str(body.get("model") or "").strip()
    if not model:
        return JSONResponse({"error": "model_required"}, status_code=422)
    try:
        ctx.harness_profiles.get(harness_id)
    except (HarnessConfigError, KeyError):
        return JSONResponse(
            {"error": "unknown_harness", "harness_id": harness_id},
            status_code=404,
        )
    try:
        ctx.harness_profiles.resolve(harness_id, connection_name)
    except (HarnessConfigError, KeyError):
        return JSONResponse(
            {
                "error": "unknown_harness_connection",
                "harness_id": harness_id,
                "connection": connection_name,
            },
            status_code=404,
        )
    if not _pin_model_allowed(model, ctx.observer.get_slots(), ctx.upstreams, extra_ids=ctx.gpu_pin_ids):
        return JSONResponse(
            {
                "error": "unknown_pin_target",
                "model": model,
                "detail": "No matching slot, upstream, or live alias is configured.",
            },
            status_code=422,
        )
    try:
        ctx.harness_profiles, _backup = ctx.harness_profiles.set_connection_model(
            harness_id, connection_name, model
        )
        ctx.harness_activity.pop(f"{harness_id}/{connection_name}", None)
        profile = ctx.harness_profiles.get(harness_id)
    except HarnessConfigError as exc:
        return JSONResponse(
            {"error": "invalid_harness_config", "detail": str(exc)},
            status_code=422,
        )
    return JSONResponse(harness_manifest(ctx, profile))


async def harness_detail(ctx: RouterContext, request: Request) -> JSONResponse:
    try:
        profile = ctx.harness_profiles.get(request.path_params.get("harness_id", ""))
    except (HarnessConfigError, KeyError):
        return JSONResponse(
            {"error": "unknown_harness", "harness_id": request.path_params.get("harness_id")},
            status_code=404,
        )
    return JSONResponse(harness_manifest(ctx, profile))


async def harness_models(ctx: RouterContext, request: Request) -> JSONResponse:
    resolved = resolve_harness_request(ctx, request)
    if isinstance(resolved, JSONResponse):
        return resolved
    profile, connection = resolved
    mark_harness_activity(ctx, profile, connection, "connected")
    capabilities = _capabilities_for_pinned_model(
        connection.model, ctx.observer.get_slots(), ctx.upstreams
    )
    gpu_status = ctx.hermes_watcher.snapshot() if ctx.hermes_watcher is not None else {}
    model_ids = ["local"]
    if ctx.gpu_cfg is not None:
        for extra in ("ornith", "uncensored"):
            if extra not in model_ids:
                model_ids.append(extra)
    return JSONResponse({
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": profile.harness_id,
                "capabilities": capabilities,
                "context_size": gpu_status.get("effective_n_ctx") if profile.harness_id == "hermes" else None,
                "meta": {
                    "harness_id": profile.harness_id,
                    "connection": connection.name,
                    "pinned_model": connection.model,
                    "capabilities": capabilities,
                    **(
                        {
                            "selected_model": gpu_status.get("desired"),
                            "loaded_profile": gpu_status.get("loaded"),
                            "configured_n_ctx": gpu_status.get("configured_n_ctx"),
                            "effective_n_ctx": gpu_status.get("effective_n_ctx"),
                            "context_source": gpu_status.get("context_source"),
                        }
                        if profile.harness_id == "hermes"
                        else {}
                    ),
                },
            }
            for model_id in model_ids
        ],
    })


async def harness_chat(ctx: RouterContext, request: Request) -> Response:
    resolved = resolve_harness_request(ctx, request)
    if isinstance(resolved, JSONResponse):
        return resolved
    profile, connection = resolved
    request.state.harness_connection = {
        "harness_id": profile.harness_id,
        "connection": connection.name,
        "model": connection.model,
    }
    mark_harness_activity(ctx, profile, connection, "seen")
    response = await handle_chat_completions(ctx, request)
    if getattr(response, "status_code", 500) < 400:
        app_id = str(request.headers.get("x-app-id") or "").strip().lower()
        identified_client = bool(app_id) and app_id in {
            str(profile.harness_id).lower(),
            str(profile.kind).lower(),
        }
        mark_harness_activity(
            ctx, profile, connection, "verified", client_verified=identified_client
        )
    return response


async def dashboard_page(ctx: RouterContext, request: Request) -> HTMLResponse:
    from local_model_router.dashboard import dashboard_html

    client_host = request.client.host if request.client else ""
    token = ctx.setup_token if client_host in {"127.0.0.1", "::1", "localhost", "testclient"} else ""
    return HTMLResponse(
        dashboard_html(setup_token=token),
        headers=_UI_SECURITY_HEADERS,
    )


async def dashboard_icon_file(request: Request) -> Response:
    from local_model_router.dashboard import dashboard_icon

    path = dashboard_icon(str(request.path_params.get("name") or ""))
    if path is None:
        return Response(status_code=404)
    return FileResponse(path, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


async def dashboard_asset_file(request: Request) -> Response:
    from local_model_router.dashboard import dashboard_asset

    resolved = dashboard_asset(str(request.path_params.get("name") or ""))
    if resolved is None:
        return Response(status_code=404)
    path, media_type = resolved
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


def build_harness_routes(ctx: RouterContext) -> list[Route]:
    return [
        Route("/harnesses", ctx.protected(ctx.bind(harnesses_list))),
        Route("/harnesses", ctx.protected(ctx.bind(harness_create)), methods=["POST"]),
        Route(
            "/harnesses/{harness_id}/connections/{connection}",
            ctx.protected(ctx.bind(harness_pin)),
            methods=["PATCH"],
        ),
        Route("/harnesses/{harness_id}/v1/models", ctx.protected(ctx.bind(harness_models))),
        Route(
            "/harnesses/{harness_id}/v1/chat/completions",
            ctx.protected(ctx.bind(harness_chat)),
            methods=["POST"],
        ),
        Route("/harnesses/{harness_id}/{connection}/v1/models", ctx.protected(ctx.bind(harness_models))),
        Route(
            "/harnesses/{harness_id}/{connection}/v1/chat/completions",
            ctx.protected(ctx.bind(harness_chat)),
            methods=["POST"],
        ),
        Route("/harnesses/{harness_id}", ctx.protected(ctx.bind(harness_detail))),
        Route("/ui", ctx.bind(dashboard_page)),
        Route("/ui/assets/{name}", dashboard_asset_file),
        Route("/ui/icons/{name}", dashboard_icon_file),
    ]
