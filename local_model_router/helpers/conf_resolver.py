"""Resolve the standalone router configuration from explicit safe roots."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

CONF_FILENAME = "llama_cpp_servers.yaml"
ENV_CONFIG = "A0_LMM_ROUTER_CONFIG"
ENV_EXTRA_ROOTS = "A0_LMM_ROUTER_CONF_ALLOW_ROOTS"


def plugin_root(caller_file: str | None = None) -> Path:
    """Return the repository directory containing ``local_model_router``."""
    if caller_file:
        here = Path(caller_file).resolve()
        for parent in (here, *here.parents):
            if parent.name == "local_model_router" and parent.is_dir():
                return parent.parent
    return Path(__file__).resolve().parents[2]


def _safe_resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def managed_conf_path() -> Path:
    home = os.environ.get("IMPERIUM_HOME", "").strip()
    if home:
        return _safe_resolve(Path(home) / "conf" / CONF_FILENAME)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if os.name == "nt" and local_app_data:
        return _safe_resolve(Path(local_app_data) / "Imperium" / "conf" / CONF_FILENAME)
    return _safe_resolve(Path.home() / ".imperium" / "conf" / CONF_FILENAME)


def allowed_conf_roots(caller_file: str | None = None) -> list[Path]:
    roots = [
        plugin_root(caller_file) / "conf",
        managed_conf_path().parent,
        Path.cwd(),
        Path(tempfile.gettempdir()),
    ]
    extra = os.environ.get(ENV_EXTRA_ROOTS, "").strip()
    if extra:
        roots.extend(Path(item) for item in extra.split(os.pathsep) if item.strip())
    return list(dict.fromkeys(_safe_resolve(root) for root in roots))


def is_safe_conf_path(path: str | Path, caller_file: str | None = None) -> bool:
    resolved = _safe_resolve(path)
    return resolved.name == CONF_FILENAME and any(
        resolved == root or root in resolved.parents
        for root in allowed_conf_roots(caller_file)
    )


def standard_conf_candidates(caller_file: str | None = None) -> list[Path]:
    return [
        _safe_resolve(plugin_root(caller_file) / "conf" / CONF_FILENAME),
        managed_conf_path(),
    ]


def resolve_conf_path(caller_file: str | None = None, *, allow_missing: bool = True) -> str:
    """Use a safe environment override, otherwise the repository config."""
    env_value = os.environ.get(ENV_CONFIG, "").strip()
    if env_value:
        env_path = _safe_resolve(env_value)
        if (env_path.exists() or allow_missing) and is_safe_conf_path(env_path, caller_file):
            return str(env_path)

    candidates = standard_conf_candidates(caller_file)
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    if allow_missing:
        return str(candidates[-1])
    raise FileNotFoundError(f"{CONF_FILENAME} not found")


def describe_allowed_roots(caller_file: str | None = None) -> list[str]:
    return [str(path) for path in allowed_conf_roots(caller_file)]
