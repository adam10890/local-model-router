"""
Entry point for the lmm-router standalone observer service.

    python -m local_model_router.service                          # 127.0.0.1:9000
    OBSERVER_PORT=9000 python -m local_model_router.service       # custom port
    A0_LMM_ROUTER_CONFIG=/path python -m local_model_router.service  # custom config
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psutil
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


def router_process_state_path() -> Path:
    home = os.environ.get("IMPERIUM_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve(strict=False) / "state" / "router-process.json"
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) / "Imperium" if os.name == "nt" and local else Path.home() / ".imperium"
    return base / "state" / "router-process.json"


def _write_router_process_state(host: str, port: int, config_path: str) -> Path:
    process = psutil.Process(os.getpid())
    path = router_process_state_path()
    payload = {
        "schema_version": 1,
        "pid": process.pid,
        "process_executable": process.exe(),
        "process_created_at": process.create_time(),
        "host": host,
        "port": port,
        "config_path": str(Path(config_path).resolve(strict=False)),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _remove_router_process_state(path: Path) -> None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if int(state.get("pid") or 0) == os.getpid():
            path.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return


def main() -> None:
    host = os.environ.get("OBSERVER_HOST", "127.0.0.1")
    port = int(os.environ.get("OBSERVER_PORT", "9000"))
    config_path = resolve_conf_path(__file__)

    _validate_bind_auth(host)
    app = create_app(config_path)
    state_path = _write_router_process_state(host, port, config_path)
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        _remove_router_process_state(state_path)


if __name__ == "__main__":
    main()
