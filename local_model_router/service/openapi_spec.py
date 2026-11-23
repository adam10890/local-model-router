"""Minimal OpenAPI 3.1.0 schema builder from Imperium's live Starlette routes."""
from __future__ import annotations

import re
from typing import Any, Sequence

from starlette.routing import Route

from .routing_intent import RoutingIntentRequest

_PATH_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]+)?\}")

_PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/.well-known/agent-card.json",
        "/ui",
        "/ui/assets/{name}",
        "/ui/icons/{name}",
    }
)

_OPERATION_REFS: dict[tuple[str, str], tuple[str | None, str]] = {
    ("/v1/chat/completions", "POST"): ("ChatCompletionRequest", "ChatCompletionResponse"),
    ("/harnesses/{harness_id}/v1/chat/completions", "POST"): (
        "ChatCompletionRequest",
        "ChatCompletionResponse",
    ),
    ("/harnesses/{harness_id}/{connection}/v1/chat/completions", "POST"): (
        "ChatCompletionRequest",
        "ChatCompletionResponse",
    ),
    ("/v1/embeddings", "POST"): ("EmbeddingRequest", "EmbeddingResponse"),
    ("/v1/models", "GET"): (None, "ModelList"),
    ("/harnesses/{harness_id}/v1/models", "GET"): (None, "ModelList"),
    ("/harnesses/{harness_id}/{connection}/v1/models", "GET"): (None, "ModelList"),
    ("/routing/request", "POST"): ("RoutingIntentRequest", "RoutingDecision"),
    ("/harnesses", "GET"): (None, "HarnessListResponse"),
    ("/health", "GET"): (None, "HealthResponse"),
}


def _normalize_path(raw_path: str) -> tuple[str, list[dict[str, Any]]]:
    params: list[dict[str, Any]] = []

    def _repl(match: re.Match[str]) -> str:
        name = match.group(1)
        params.append(
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        )
        return "{" + name + "}"

    normalized = _PATH_PARAM_RE.sub(_repl, raw_path)
    return normalized, params


def _component_schemas() -> dict[str, Any]:
    routing_schema = RoutingIntentRequest.model_json_schema()
    return {
        "ErrorDetail": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "type": {"type": "string"},
                "param": {"type": ["string", "null"]},
                "code": {"type": ["string", "null"]},
            },
            "required": ["message", "type"],
        },
        "ErrorEnvelope": {
            "type": "object",
            "properties": {"error": {"$ref": "#/components/schemas/ErrorDetail"}},
            "required": ["error"],
        },
        "ChatMessage": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "content": {},
            },
            "required": ["role", "content"],
        },
        "ChatCompletionRequest": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "messages": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/ChatMessage"},
                },
                "stream": {"type": "boolean"},
                "temperature": {"type": "number"},
                "max_tokens": {"type": "integer"},
                "response_format": {"type": "object"},
            },
            "required": ["messages"],
        },
        "ChatCompletionResponse": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "object": {"type": "string"},
                "model": {"type": "string"},
                "choices": {"type": "array", "items": {"type": "object"}},
                "usage": {
                    "type": "object",
                    "properties": {
                        "prompt_tokens": {"type": "integer"},
                        "completion_tokens": {"type": "integer"},
                        "total_tokens": {"type": "integer"},
                    },
                },
            },
            "required": ["choices"],
        },
        "EmbeddingRequest": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "input": {},
            },
            "required": ["input"],
        },
        "EmbeddingResponse": {
            "type": "object",
            "properties": {
                "object": {"type": "string", "enum": ["list"]},
                "data": {"type": "array", "items": {"type": "object"}},
                "model": {"type": "string"},
            },
            "required": ["object", "data"],
        },
        "ModelList": {
            "type": "object",
            "properties": {
                "object": {"type": "string", "enum": ["list"]},
                "data": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["object", "data"],
        },
        "HealthResponse": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "service": {"type": "string"},
                "version": {"type": "string"},
            },
            "required": ["status", "version"],
        },
        "HarnessManifest": {
            "type": "object",
            "properties": {
                "harness_id": {"type": "string"},
                "display_name": {"type": "string"},
                "kind": {"type": "string"},
                "connections": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["harness_id", "connections"],
        },
        "HarnessListResponse": {
            "type": "object",
            "properties": {
                "harnesses": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/HarnessManifest"},
                }
            },
            "required": ["harnesses"],
        },
        "RoutingIntentRequest": routing_schema,
        "RoutingDecision": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "selected_model": {"type": ["string", "null"]},
                "reason_codes": {"type": "array", "items": {"type": "string"}},
            },
        },
    }


def _security_for(path: str) -> list[dict[str, list[str]]]:
    if path in _PUBLIC_PATHS:
        return []
    if path.startswith("/setup/"):
        return [{"SetupToken": []}]
    return [{"BearerAuth": []}]


def build_openapi_spec(routes: Sequence[Any], version: str) -> dict[str, Any]:
    """Build an OpenAPI 3.1.0 document from registered Starlette routes."""
    paths: dict[str, dict[str, Any]] = {}
    for route in routes:
        if not isinstance(route, Route):
            continue
        methods = sorted(m for m in (route.methods or set()) if m != "HEAD")
        if not methods:
            continue
        norm_path, params = _normalize_path(route.path)
        path_slug = re.sub(r"[^a-zA-Z0-9]+", "_", norm_path).strip("_") or "root"
        path_item = paths.setdefault(norm_path, {})
        for method in methods:
            req_ref, resp_ref = _OPERATION_REFS.get((norm_path, method), (None, None))
            security = _security_for(norm_path)
            responses: dict[str, Any] = {
                "200": (
                    {
                        "description": "Successful response",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": f"#/components/schemas/{resp_ref}"}
                            }
                        },
                    }
                    if resp_ref
                    else {"description": "Successful response"}
                ),
            }
            if security:
                responses["401"] = {
                    "description": "Authentication required",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                        }
                    },
                }
            op: dict[str, Any] = {
                "operationId": f"{method.lower()}_{path_slug}",
                "security": security,
                "responses": responses,
            }
            if params:
                op["parameters"] = params
            if req_ref:
                op["requestBody"] = {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{req_ref}"}
                        }
                    },
                }
            elif method in {"POST", "PATCH", "PUT"}:
                op["requestBody"] = {
                    "content": {"application/json": {"schema": {"type": "object"}}}
                }
            path_item[method.lower()] = op

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Imperium Local Model Router API",
            "version": version,
            "description": "Local-first OpenAI-compatible router, fleet control, and harness gateway.",
        },
        "components": {
            "securitySchemes": {
                "BearerAuth": {"type": "http", "scheme": "bearer"},
                "SetupToken": {"type": "apiKey", "in": "header", "name": "X-Setup-Token"},
            },
            "schemas": _component_schemas(),
        },
        "paths": dict(sorted(paths.items())),
    }
