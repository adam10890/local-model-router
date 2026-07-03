"""Secret-free setup manifests for dedicated harness connections."""
from __future__ import annotations

from typing import Any, Dict, Optional

from .profiles import HarnessProfile


def connection_base_url(profile: HarnessProfile, connection_name: str, *, port: int = 9000) -> str:
    host = "host.docker.internal" if profile.location == "docker" else "127.0.0.1"
    connection_path = "" if set(profile.connections) == {"default"} else f"/{connection_name}"
    return f"http://{host}:{port}/harnesses/{profile.harness_id}{connection_path}/v1"


def _setup(profile: HarnessProfile, connections: list[Dict[str, Any]]) -> Dict[str, str]:
    first = connections[0]
    base_url = first["base_url"]
    if profile.kind == "pi":
        return {
            "target": "~/.pi/agent/models.json and ~/.pi/agent/settings.json",
            "format": "json",
            "content": (
                '{"providers":{"lmm-router":{"baseUrl":"' + base_url
                + '","apiKey":"${ROUTER_API_KEY:-local}","api":"openai-completions",'
                '"models":[{"id":"local","name":"LMM Router"}]}}}'
            ),
        }
    if profile.kind == "hermes":
        return {
            "target": "Hermes model provider settings (~/.hermes/config.yaml)",
            "format": "yaml",
            "content": (
                "model:\n"
                "  default: local\n"
                "  provider: lmm-router\n"
                f"  base_url: {base_url}\n"
                "  api_key: ${ROUTER_API_KEY:-local}\n"
                "providers:\n"
                "  lmm-router:\n"
                "    name: LMM Router\n"
                f"    base_url: {base_url}\n"
                "    api_key: ${ROUTER_API_KEY:-local}\n"
                "    default_model: local\n"
                "    models: [local]\n"
                "    discover_models: true"
            ),
        }
    if profile.kind == "agent_zero":
        lines = [
            f"{item['name']}: base_url={item['base_url']} model=local api_key=${{ROUTER_API_KEY:-local}}"
            for item in connections
        ]
        return {
            "target": "Agent Zero model provider settings",
            "format": "env",
            "content": "\n".join(lines),
        }
    if profile.kind == "claude_code":
        return {
            "target": "LiteLLM Proxy config plus a separate claude-local launcher",
            "format": "yaml+powershell",
            "content": (
                "# litellm-config.yaml\n"
                "model_list:\n"
                "  - model_name: local\n"
                "    litellm_params:\n"
                "      model: openai/local\n"
                f"      api_base: {base_url}\n"
                "      api_key: ${ROUTER_API_KEY:-local}\n\n"
                "# Start: litellm --config litellm-config.yaml --port 4000\n"
                "$env:ANTHROPIC_BASE_URL='http://127.0.0.1:4000'\n"
                "$env:ANTHROPIC_AUTH_TOKEN='local'\n"
                "claude --model local"
            ),
        }
    return {
        "target": "OpenAI-compatible client settings",
        "format": "env",
        "content": (
            f"OPENAI_BASE_URL={base_url}\n"
            "OPENAI_API_KEY=${ROUTER_API_KEY:-local}\n"
            "OPENAI_MODEL=local"
        ),
    }


def setup_manifest(
    profile: HarnessProfile,
    *,
    auth_required: bool,
    verification_by_connection: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    activity = verification_by_connection or {}
    connections = [
        {
            **connection.describe(),
            "client_model": "local",
            "base_url": connection_base_url(profile, connection.name),
            "endpoints": ["models", "chat/completions"],
            "verification": activity.get(
                connection.name, {"state": "not_seen", "last_seen": None}
            ),
        }
        for connection in profile.connections.values()
    ]
    seen = [item["verification"] for item in connections if item["verification"]["last_seen"]]
    state = max(seen, key=lambda item: item["last_seen"]) if seen else {
        "state": "not_seen",
        "last_seen": None,
    }
    smoke_url = connections[0]["base_url"] + "/models"
    return {
        "harness_id": profile.harness_id,
        "display_name": profile.display_name,
        "kind": profile.kind,
        "protocol": profile.protocol,
        "location": profile.location,
        "connections": connections,
        "authentication_required": auth_required,
        "setup": _setup(profile, connections),
        "smoke": f"curl {smoke_url}",
        "verification": state,
    }
