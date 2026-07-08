"""
server.py — FastMCP server entry point for lmm-router.

Exposes tools and resources over Streamable HTTP (MCP spec 2025-06-18).
Endpoint: POST/GET http://<host>:<port>/mcp

Usage:
    python -m local_model_router.mcp.server           # uses defaults (127.0.0.1:8095)
    MCP_PORT=9000 python -m local_model_router.mcp.server

Config override via llama_cpp_servers.yaml:
    mcp_server:
      enabled: true
      host: "127.0.0.1"
      port: 8095
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from mcp.server.auth.provider import AccessToken  # noqa: E402
from mcp.server.auth.settings import AuthSettings  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from local_model_router.mcp.tools import register_tools  # noqa: E402
from local_model_router.mcp.resources import register_resources  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("lmm_router.mcp")


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _token_candidate_paths() -> list[str]:
    env_path = os.environ.get("MCP_TOKEN_PATH", "").strip()
    return [env_path] if env_path else []


def _resolve_mcp_host(cfg: dict) -> str:
    """Resolve bind host with localhost-safe defaults.

    A config value of 0.0.0.0 is treated as unsafe unless the user opts into a
    public bind via MCP_BIND_PUBLIC=1. MCP_HOST remains an explicit override.
    """
    if "MCP_HOST" in os.environ:
        return os.environ["MCP_HOST"].strip() or "127.0.0.1"

    host = str(cfg.get("host", "127.0.0.1") or "127.0.0.1").strip()
    if host in {"", "0.0.0.0", "::"} and not _truthy(os.environ.get("MCP_BIND_PUBLIC")):
        return "127.0.0.1"
    return host


class StaticFileTokenVerifier:
    """Bearer-token verifier backed by one or more token files."""

    def __init__(self, token_paths: list[str] | None = None) -> None:
        self.token_paths = token_paths or _token_candidate_paths()

    def _read_expected_token(self) -> str:
        for path in self.token_paths:
            try:
                p = Path(path)
                if p.is_file():
                    return p.read_text(encoding="utf-8").strip()
            except Exception:
                continue
        return ""

    async def verify_token(self, token: str) -> AccessToken | None:
        expected = self._read_expected_token()
        if not expected or not hmac.compare_digest(token or "", expected):
            return None
        return AccessToken(token=token, client_id="local_model_router", scopes=["mcp"])


def _load_mcp_config() -> dict:
    """Read the ``mcp_server`` section from the resolved fleet YAML."""
    try:
        from local_model_router.helpers.conf_resolver import resolve_conf_path

        path = resolve_conf_path(__file__)
    except Exception:
        return {}
    if not path or not Path(path).is_file():
        return {}
    try:
        import yaml  # noqa: PLC0415

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("mcp_server", {})
    except Exception:
        return {}


def _auth_settings(host: str, port: int) -> AuthSettings:
    url_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    resource = f"http://{url_host}:{port}"
    return AuthSettings(
        issuer_url=resource,
        resource_server_url=resource,
        required_scopes=["mcp"],
    )


def create_app(
    host: str = "127.0.0.1",
    port: int = 8095,
    allow_mutating_tools: bool = False,
    enable_auth: bool = True,
) -> FastMCP:
    # host/port are passed to FastMCP at construction time; mcp>=1.10 dropped
    # them from FastMCP.run() kwargs and reads them from settings instead.
    token_verifier = StaticFileTokenVerifier() if enable_auth else None
    mcp = FastMCP(
        "lmm-router",
        instructions=(
            "Local LLM router. Use chat_completion / utility_completion for inference, "
            "get_embeddings for vectors, fleet_status to inspect slots, "
            "and admin tools only when mutating tools are explicitly enabled."
        ),
        host=host,
        port=port,
        auth=_auth_settings(host, port) if enable_auth else None,
        token_verifier=token_verifier,
    )
    register_tools(mcp, allow_mutating_tools=allow_mutating_tools)
    register_resources(mcp)
    return mcp


def main() -> None:
    cfg = _load_mcp_config()
    host = _resolve_mcp_host(cfg)
    port = int(os.environ.get("MCP_PORT", cfg.get("port", 8095)))
    allow_mutating_tools = _truthy(os.environ.get("MCP_ALLOW_MUTATING_TOOLS", cfg.get("allow_mutating_tools", False)))
    enable_auth = not _truthy(os.environ.get("MCP_DISABLE_AUTH"))

    if not cfg.get("enabled", True):
        logger.info("MCP server disabled in config (mcp_server.enabled=false). Exiting.")
        return

    if host in {"0.0.0.0", "::"}:
        logger.warning("MCP server is publicly bound on %s; keep auth enabled.", host)
    if allow_mutating_tools:
        logger.warning("MCP mutating tools are enabled. Ensure token access is tightly controlled.")

    mcp = create_app(
        host=host,
        port=port,
        allow_mutating_tools=allow_mutating_tools,
        enable_auth=enable_auth,
    )

    logger.info("Starting lmm-router MCP server on %s:%d/mcp (Streamable HTTP)", host, port)
    logger.info("Inspector: npx @modelcontextprotocol/inspector http://%s:%d/mcp", host, port)

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
