"""Deprecated legacy orchestrator endpoints gated by env policy."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .agent_orchestrator import OrchestratorError
from .context import RouterContext, legacy_orchestrator_enabled


def _orchestrator_error(exc: OrchestratorError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "detail": exc.message}, status_code=exc.status_code)


async def orchestrator_create_plan(ctx: RouterContext, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
    try:
        return JSONResponse(ctx.agent_orchestrator.create_plan(body))
    except OrchestratorError as exc:
        return _orchestrator_error(exc)


async def orchestrator_list_plans(ctx: RouterContext, request: Request) -> JSONResponse:
    return JSONResponse(ctx.agent_orchestrator.list_plans())


async def orchestrator_summary(ctx: RouterContext, request: Request) -> JSONResponse:
    return JSONResponse(ctx.agent_orchestrator.summary())


async def orchestrator_list_instances(ctx: RouterContext, request: Request) -> JSONResponse:
    plan_id = str(request.query_params.get("plan_id") or "").strip()
    try:
        return JSONResponse(ctx.agent_orchestrator.list_instances(plan_id=plan_id))
    except OrchestratorError as exc:
        return _orchestrator_error(exc)


async def orchestrator_instance_upsert(ctx: RouterContext, request: Request) -> JSONResponse:
    instance_id = str(request.path_params.get("instance_id") or "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
    try:
        return JSONResponse(ctx.agent_orchestrator.upsert_instance(instance_id, body))
    except OrchestratorError as exc:
        return _orchestrator_error(exc)


async def orchestrator_plan_detail(ctx: RouterContext, request: Request) -> JSONResponse:
    plan_id = str(request.path_params.get("plan_id") or "")
    try:
        return JSONResponse(ctx.agent_orchestrator.get_plan(plan_id))
    except OrchestratorError as exc:
        return _orchestrator_error(exc)


async def orchestrator_ticket_detail(ctx: RouterContext, request: Request) -> JSONResponse:
    ticket_id = str(request.path_params.get("ticket_id") or "")
    try:
        return JSONResponse(ctx.agent_orchestrator.get_ticket(ticket_id))
    except OrchestratorError as exc:
        return _orchestrator_error(exc)


async def orchestrator_ticket_claim(ctx: RouterContext, request: Request) -> JSONResponse:
    ticket_id = str(request.path_params.get("ticket_id") or "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
    try:
        return JSONResponse(ctx.agent_orchestrator.claim_ticket(ticket_id, body))
    except OrchestratorError as exc:
        return _orchestrator_error(exc)


async def orchestrator_ticket_log(ctx: RouterContext, request: Request) -> JSONResponse:
    ticket_id = str(request.path_params.get("ticket_id") or "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
    try:
        return JSONResponse(ctx.agent_orchestrator.append_ticket_log(ticket_id, body))
    except OrchestratorError as exc:
        return _orchestrator_error(exc)


async def _orchestrator_ticket_finish(ctx: RouterContext, request: Request, status: str) -> JSONResponse:
    ticket_id = str(request.path_params.get("ticket_id") or "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
    try:
        return JSONResponse(
            ctx.agent_orchestrator.finish_claimed_ticket(ticket_id, body, status=status)
        )
    except OrchestratorError as exc:
        return _orchestrator_error(exc)


async def orchestrator_ticket_complete(ctx: RouterContext, request: Request) -> JSONResponse:
    return await _orchestrator_ticket_finish(ctx, request, "completed")


async def orchestrator_ticket_block(ctx: RouterContext, request: Request) -> JSONResponse:
    return await _orchestrator_ticket_finish(ctx, request, "blocked")


async def orchestrator_ticket_submit(ctx: RouterContext, request: Request) -> JSONResponse:
    ticket_id = str(request.path_params.get("ticket_id") or "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
    try:
        return JSONResponse(ctx.agent_orchestrator.submit_ticket(ticket_id, body))
    except OrchestratorError as exc:
        return _orchestrator_error(exc)



def build_orchestrator_routes(ctx: RouterContext) -> list[Route]:
    if not legacy_orchestrator_enabled():
        return []
    wrap = lambda fn: ctx.protected(ctx.deprecated(ctx.bind(fn)))  # noqa: E731
    return [
        Route("/orchestrator/plans", wrap(orchestrator_create_plan), methods=["POST"]),
        Route("/orchestrator/plans", wrap(orchestrator_list_plans)),
        Route("/orchestrator/summary", wrap(orchestrator_summary)),
        Route("/orchestrator/instances", wrap(orchestrator_list_instances)),
        Route("/orchestrator/instances/{instance_id}", wrap(orchestrator_instance_upsert), methods=["POST"]),
        Route("/orchestrator/plans/{plan_id}", wrap(orchestrator_plan_detail)),
        Route("/orchestrator/tickets/{ticket_id}/claim", wrap(orchestrator_ticket_claim), methods=["POST"]),
        Route("/orchestrator/tickets/{ticket_id}/log", wrap(orchestrator_ticket_log), methods=["POST"]),
        Route("/orchestrator/tickets/{ticket_id}/complete", wrap(orchestrator_ticket_complete), methods=["POST"]),
        Route("/orchestrator/tickets/{ticket_id}/block", wrap(orchestrator_ticket_block), methods=["POST"]),
        Route("/orchestrator/tickets/{ticket_id}/submit", wrap(orchestrator_ticket_submit), methods=["POST"]),
        Route("/orchestrator/tickets/{ticket_id}", wrap(orchestrator_ticket_detail)),
    ]
