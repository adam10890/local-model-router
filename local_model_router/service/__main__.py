"""
Entry point for the lmm-router standalone observer service.

    python -m local_model_router.service                          # 127.0.0.1:9000
    OBSERVER_PORT=9000 python -m local_model_router.service       # custom port
    A0_LMM_ROUTER_CONFIG=/path python -m local_model_router.service  # custom config
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

from .app import create_app
try:
    from local_model_router.helpers.conf_resolver import resolve_conf_path
except ImportError:
    _PLUGIN_ROOT = Path(__file__).resolve().parents[1]
    if str(_PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_ROOT))
    from local_model_router.helpers.conf_resolver import resolve_conf_path

_API_KEY_ENV = "A0_LMM_ROUTER_API_KEY"
_ALLOW_PUBLIC_NO_AUTH_ENV = "A0_LMM_ROUTER_ALLOW_PUBLIC_NO_AUTH"
_LOCAL_BINDS = {"127.0.0.1", "localhost", "::1"}
_TRUTHY = {"1", "true", "yes", "on"}


def _has_api_key() -> bool:
    return bool(os.environ.get(_API_KEY_ENV, "").strip())


def _allows_public_no_auth() -> bool:
    return os.environ.get(_ALLOW_PUBLIC_NO_AUTH_ENV, "").strip().lower() in _TRUTHY


def _is_public_bind(host: str) -> bool:
    normalized = (host or "127.0.0.1").strip().lower().strip("[]")
    if normalized in _LOCAL_BINDS or normalized.startswith("127."):
        return False
    return True


def _validate_bind_auth(host: str) -> None:
    if not _is_public_bind(host):
        return
    if _has_api_key() or _allows_public_no_auth():
        return
    raise RuntimeError(
        "Refusing public bind without auth. Set A0_LMM_ROUTER_API_KEY or "
        "set A0_LMM_ROUTER_ALLOW_PUBLIC_NO_AUTH=1 to acknowledge an intentional no-auth public bind."
    )


def main() -> None:
    host = os.environ.get("OBSERVER_HOST", "127.0.0.1")
    port = int(os.environ.get("OBSERVER_PORT", "9000"))
    config_path = resolve_conf_path(__file__)

    _validate_bind_auth(host)
    app = create_app(config_path)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
