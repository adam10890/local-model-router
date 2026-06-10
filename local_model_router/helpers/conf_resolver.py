"""Safe config-path resolution for a0_lmm_router.

The plugin accepts ``A0_LMM_ROUTER_CONFIG`` for portable deployments, but API
handlers must not blindly write to an arbitrary path from the environment. This
module is the single place that resolves that env override and constrains it to
known config roots.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

CONF_FILENAME = "llama_cpp_servers.yaml"
ENV_CONFIG = "A0_LMM_ROUTER_CONFIG"
ENV_EXTRA_ROOTS = "A0_LMM_ROUTER_CONF_ALLOW_ROOTS"


def plugin_root(caller_file: str | None = None) -> Path:
    """Repo root: the directory containing the local_model_router package."""
    if caller_file:
        here = Path(caller_file).resolve()
        for parent in [here, *here.parents]:
            if parent.name == "local_model_router" and parent.is_dir():
                return parent.parent
    return Path(__file__).resolve().parents[2]


def _is_container_runtime() -> bool:
    return Path("/a0").exists()


def _safe_resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def allowed_conf_roots(caller_file: str | None = None) -> list[Path]:
    root = plugin_root(caller_file)
    roots = [
        Path("/a0/conf"),
        Path("/app/local_model_router/conf"),
        Path("/app/conf"),
        root / "conf",
    ]

    # Local unit tests and desktop dev runs use temp config files. Do not add
    # temp roots inside the A0 container where env-controlled paths are higher
    # risk.
    if not _is_container_runtime():
        roots.extend([
            Path.cwd(),
            Path(tempfile.gettempdir()),
        ])

    extra = os.environ.get(ENV_EXTRA_ROOTS, "").strip()
    if extra:
        roots.extend(Path(p) for p in extra.split(os.pathsep) if p.strip())

    out: list[Path] = []
    seen: set[str] = set()
    for item in roots:
        resolved = _safe_resolve(item)
        key = str(resolved).lower()
        if key not in seen:
            seen.add(key)
            out.append(resolved)
    return out


def is_safe_conf_path(path: str | Path, caller_file: str | None = None) -> bool:
    resolved = _safe_resolve(path)
    if resolved.name != CONF_FILENAME:
        return False
    return any(_under(resolved, root) for root in allowed_conf_roots(caller_file))


def standard_conf_candidates(caller_file: str | None = None) -> list[Path]:
    root = plugin_root(caller_file)
    candidates = [
        Path("/a0/conf") / CONF_FILENAME,
        Path("/app/local_model_router/conf") / CONF_FILENAME,
        root / "conf" / CONF_FILENAME,
        Path("/app/conf") / CONF_FILENAME,
    ]
    out: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = _safe_resolve(candidate)
        key = str(resolved).lower()
        if key not in seen:
            seen.add(key)
            out.append(resolved)
    return out


def resolve_conf_path(caller_file: str | None = None, *, allow_missing: bool = True) -> str:
    """Resolve the router YAML path, honoring safe env overrides.

    Unsafe env values are ignored rather than returned. If no candidate exists,
    the plugin-local config path is returned when ``allow_missing`` is true so
    callers that tolerate missing configs can keep their existing behavior.
    """
    env_conf = os.environ.get(ENV_CONFIG, "").strip()
    if env_conf:
        env_path = _safe_resolve(env_conf)
        if env_path.exists() and is_safe_conf_path(env_path, caller_file):
            return str(env_path)

    candidates = standard_conf_candidates(caller_file)
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    if allow_missing and candidates:
        root = plugin_root(caller_file)
        return str(_safe_resolve(root / "conf" / CONF_FILENAME))
    raise FileNotFoundError(f"{CONF_FILENAME} not found in safe config roots")


def describe_allowed_roots(caller_file: str | None = None) -> list[str]:
    return [str(path) for path in allowed_conf_roots(caller_file)]
