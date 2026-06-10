"""Max-feasible context planning for llama.cpp Router Mode.

The values in Agent Zero model settings are minimum operating windows, not
caps.  This module plans the largest safe llama.cpp context for each alias,
then exposes a smaller effective budget used by the compression guard.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

try:
    from local_model_router.helpers.context_calculator import read_gguf_metadata
except ImportError:  # pragma: no cover - exercised in Agent Zero plugin import mode
    from local_model_router.helpers.context_calculator import read_gguf_metadata  # type: ignore


DEFAULT_EFFECTIVE_CTX_RATIO = 0.70
DEFAULT_VRAM_SAFETY_MARGIN_GB = 2.5
DEFAULT_CONTEXT_BUCKETS = (8192, 16384, 32768, 65536, 98304, 131072, 196608, 262144)

ROLE_MIN_ENV = {
    "chat": ("A0_LMM_MIN_CTX_CHAT", "CHAT_CTX_SIZE", "LMM_CHAT_CTX_SIZE"),
    "utility": ("A0_LMM_MIN_CTX_UTILITY", "UTILITY_CTX_SIZE", "LMM_UTILITY_CTX_SIZE"),
    "embed": ("A0_LMM_MIN_CTX_EMBED", "EMBED_CTX_SIZE", "LMM_EMBED_CTX_SIZE"),
    "embedding": ("A0_LMM_MIN_CTX_EMBED", "EMBED_CTX_SIZE", "LMM_EMBED_CTX_SIZE"),
}

ROLE_DEFAULT_MIN_CTX = {
    "chat": 65536,
    "utility": 16384,
    "embed": 8192,
    "embedding": 8192,
}

ROLE_RESPONSE_RESERVE = {
    "chat": 8192,
    "utility": 4096,
    "embed": 512,
    "embedding": 512,
}

KV_BYTES = {
    "f32": 4.0,
    "f16": 2.0,
    "bf16": 2.0,
    "q8_0": 1.0,
    "q5_0": 0.625,
    "q5_1": 0.75,
    "q4_0": 0.5,
    "q4_1": 0.5625,
    "iq4_nl": 0.5,
}


@dataclass(frozen=True)
class ContextPlan:
    alias: str
    role: str
    model_path: str
    min_ctx: int
    hard_ctx: int
    effective_ctx: int
    response_reserve: int
    effective_ratio: float
    n_ctx_train: Optional[int]
    planned_vram_gb: Optional[float]
    kv_cache_gb: Optional[float]
    no_capacity: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "role": self.role,
            "model_path": self.model_path,
            "min_ctx": self.min_ctx,
            "hard_ctx": self.hard_ctx,
            "effective_ctx": self.effective_ctx,
            "response_reserve": self.response_reserve,
            "effective_ratio": self.effective_ratio,
            "n_ctx_train": self.n_ctx_train,
            "planned_vram_gb": self.planned_vram_gb,
            "kv_cache_gb": self.kv_cache_gb,
            "no_capacity": self.no_capacity,
            "reason": self.reason,
        }


def normalize_role(role: str) -> str:
    value = (role or "chat").strip().lower()
    if value == "embedding":
        return "embed"
    return value


def _env_get(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip()


def _env_int(env: Mapping[str, str], names: tuple[str, ...], default: int, minimum: int = 1) -> int:
    for name in names:
        raw = _env_get(env, name)
        if not raw:
            continue
        try:
            return max(minimum, int(raw))
        except ValueError:
            continue
    return max(minimum, default)


def _env_float(env: Mapping[str, str], names: tuple[str, ...], default: float) -> float:
    for name in names:
        raw = _env_get(env, name)
        if not raw:
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return default


def min_ctx_for_role(role: str, env: Mapping[str, str] | None = None) -> int:
    role_key = normalize_role(role)
    env = env or os.environ
    return _env_int(
        env,
        ROLE_MIN_ENV.get(role_key, (f"A0_LMM_MIN_CTX_{role_key.upper()}",)),
        ROLE_DEFAULT_MIN_CTX.get(role_key, 8192),
        minimum=1024,
    )


def effective_ratio_for_role(role: str, env: Mapping[str, str] | None = None) -> float:
    role_key = normalize_role(role)
    env = env or os.environ
    ratio = _env_float(
        env,
        (f"A0_LMM_EFFECTIVE_CTX_RATIO_{role_key.upper()}", "A0_LMM_EFFECTIVE_CTX_RATIO"),
        DEFAULT_EFFECTIVE_CTX_RATIO,
    )
    return min(0.95, max(0.10, ratio))


def response_reserve_for_role(role: str, env: Mapping[str, str] | None = None) -> int:
    role_key = normalize_role(role)
    env = env or os.environ
    return _env_int(
        env,
        (f"A0_LMM_RESPONSE_TOKEN_RESERVE_{role_key.upper()}", "A0_LMM_RESPONSE_TOKEN_RESERVE"),
        ROLE_RESPONSE_RESERVE.get(role_key, 2048),
        minimum=0,
    )


def vram_safety_margin_gb(env: Mapping[str, str] | None = None) -> float:
    env = env or os.environ
    return max(0.0, _env_float(env, ("A0_LMM_VRAM_SAFETY_MARGIN_GB",), DEFAULT_VRAM_SAFETY_MARGIN_GB))


def detect_total_vram_gb() -> Optional[float]:
    """Best-effort total VRAM detection using nvidia-smi."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    values = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            values.append(float(line) / 1024.0)
        except ValueError:
            continue
    return sum(values) if values else None


def available_vram_from_env(env: Mapping[str, str] | None = None) -> Optional[float]:
    env = env or os.environ
    explicit = _env_float(env, ("A0_LMM_AVAILABLE_VRAM_GB", "A0_LMM_TOTAL_VRAM_GB"), -1.0)
    if explicit > 0:
        return explicit
    return detect_total_vram_gb()


def _kv_element_bytes(cache_type: str) -> float:
    return KV_BYTES.get((cache_type or "f16").strip().lower(), 2.0)


def kv_cache_gb(
    ctx_size: int,
    n_layer: Optional[int],
    n_embd: Optional[int],
    *,
    cache_type_k: str = "f16",
    cache_type_v: str = "f16",
) -> Optional[float]:
    if not ctx_size or not n_layer or not n_embd:
        return None
    bytes_per_token = n_layer * n_embd * (
        _kv_element_bytes(cache_type_k) + _kv_element_bytes(cache_type_v)
    )
    return (ctx_size * bytes_per_token) / (1024**3)


def _round_down_context(value: int, min_ctx: int, n_ctx_train: Optional[int]) -> int:
    ceiling = n_ctx_train if n_ctx_train and n_ctx_train > 0 else DEFAULT_CONTEXT_BUCKETS[-1]
    max_allowed = max(1024, min(value, ceiling))
    candidates = [b for b in DEFAULT_CONTEXT_BUCKETS if b <= max_allowed]
    if candidates:
        chosen = candidates[-1]
    else:
        chosen = (max_allowed // 1024) * 1024
    return max(1024, chosen, min_ctx if max_allowed >= min_ctx else chosen)


def _metadata_value(metadata: Mapping[str, Any], name: str) -> Optional[int]:
    value = metadata.get(name)
    try:
        if value is None:
            return None
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def plan_model_context(
    *,
    alias: str,
    role: str,
    model_path: str,
    min_ctx: Optional[int] = None,
    available_vram_gb: Optional[float] = None,
    other_resident_vram_gb: float = 0.0,
    cache_type_k: str = "q8_0",
    cache_type_v: str = "q8_0",
    parallel: int = 1,
    effective_ratio: Optional[float] = None,
    response_reserve: Optional[int] = None,
    vram_margin_gb: Optional[float] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    env: Mapping[str, str] | None = None,
) -> ContextPlan:
    """Plan the largest feasible per-request context for a model alias."""
    env = env or os.environ
    role_key = normalize_role(role)
    role_min = min_ctx if min_ctx is not None else min_ctx_for_role(role_key, env)
    ratio = effective_ratio if effective_ratio is not None else effective_ratio_for_role(role_key, env)
    reserve = response_reserve if response_reserve is not None else response_reserve_for_role(role_key, env)
    margin = vram_margin_gb if vram_margin_gb is not None else vram_safety_margin_gb(env)
    parallel = max(1, int(parallel or 1))

    meta: Mapping[str, Any] = metadata or read_gguf_metadata(model_path)
    n_ctx_train = _metadata_value(meta, "n_ctx_train")
    n_layer = _metadata_value(meta, "n_layer")
    n_embd = _metadata_value(meta, "n_embd")
    try:
        file_size_gb = float(meta.get("file_size_gb") or 0.0)
    except (TypeError, ValueError):
        file_size_gb = 0.0

    model_ceiling = n_ctx_train if n_ctx_train and n_ctx_train > 0 else role_min
    fallback_ctx = role_min if not n_ctx_train else _round_down_context(
        model_ceiling,
        role_min,
        n_ctx_train,
    )

    if available_vram_gb is None or available_vram_gb <= 0 or file_size_gb <= 0:
        hard_ctx = min(fallback_ctx, model_ceiling)
        effective_ctx = int(hard_ctx * ratio)
        return ContextPlan(
            alias=alias,
            role=role_key,
            model_path=model_path,
            min_ctx=role_min,
            hard_ctx=hard_ctx,
            effective_ctx=effective_ctx,
            response_reserve=reserve,
            effective_ratio=ratio,
            n_ctx_train=n_ctx_train,
            planned_vram_gb=None,
            kv_cache_gb=None,
            no_capacity=hard_ctx < role_min,
            reason="metadata or VRAM unavailable; using model ceiling/minimum fallback",
        )

    weights_gb = file_size_gb * 1.05
    overhead_gb = 0.75
    usable_gb = available_vram_gb - other_resident_vram_gb - margin - weights_gb - overhead_gb
    if usable_gb <= 0:
        effective_ctx = int(role_min * ratio)
        return ContextPlan(
            alias=alias,
            role=role_key,
            model_path=model_path,
            min_ctx=role_min,
            hard_ctx=role_min,
            effective_ctx=effective_ctx,
            response_reserve=reserve,
            effective_ratio=ratio,
            n_ctx_train=n_ctx_train,
            planned_vram_gb=round(weights_gb + overhead_gb, 2),
            kv_cache_gb=0.0,
            no_capacity=True,
            reason="not enough VRAM for weights plus safety margin",
        )

    kv_for_8k = kv_cache_gb(
        8192,
        n_layer,
        n_embd,
        cache_type_k=cache_type_k,
        cache_type_v=cache_type_v,
    )
    if kv_for_8k is None:
        kv_for_8k = (file_size_gb / 10.0) * 0.5
    activation_for_8k = (file_size_gb / 10.0) * 0.3
    per_token_gb = (kv_for_8k + activation_for_8k) / 8192.0
    if per_token_gb <= 0:
        max_ctx_from_vram = model_ceiling
    else:
        max_total_ctx = int(usable_gb / per_token_gb)
        max_ctx_from_vram = max_total_ctx // parallel

    raw_ctx = min(max_ctx_from_vram, model_ceiling)
    hard_ctx = _round_down_context(raw_ctx, role_min, n_ctx_train)
    planned_kv = kv_cache_gb(
        hard_ctx * parallel,
        n_layer,
        n_embd,
        cache_type_k=cache_type_k,
        cache_type_v=cache_type_v,
    )
    activation_gb = (hard_ctx * parallel / 8192.0) * activation_for_8k
    planned_vram = weights_gb + overhead_gb + (planned_kv or kv_for_8k) + activation_gb
    no_capacity = hard_ctx < role_min or raw_ctx < role_min
    effective_ctx = int(hard_ctx * ratio)

    return ContextPlan(
        alias=alias,
        role=role_key,
        model_path=model_path,
        min_ctx=role_min,
        hard_ctx=hard_ctx,
        effective_ctx=effective_ctx,
        response_reserve=reserve,
        effective_ratio=ratio,
        n_ctx_train=n_ctx_train,
        planned_vram_gb=round(planned_vram, 2),
        kv_cache_gb=round(planned_kv, 2) if planned_kv is not None else None,
        no_capacity=no_capacity,
        reason=(
            f"VRAM allows about {max_ctx_from_vram} tokens per slot; "
            f"model ceiling {model_ceiling}; planned {hard_ctx}"
        ),
    )


def context_status_from_slot(slot: Mapping[str, Any], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return Fleet Manager context telemetry for a configured slot."""
    env = env or os.environ
    role = normalize_role(str(slot.get("role") or "chat"))
    hard_ctx = int(slot.get("context_size") or min_ctx_for_role(role, env))
    ratio = effective_ratio_for_role(role, env)
    reserve = response_reserve_for_role(role, env)
    return {
        "slot_id": slot.get("id"),
        "role": role,
        "model_id": slot.get("model_id"),
        "min_ctx": min_ctx_for_role(role, env),
        "hard_ctx": hard_ctx,
        "effective_ctx": int(hard_ctx * ratio),
        "response_reserve": reserve,
        "effective_ratio": ratio,
        "occupancy": None,
        "resident": None,
        "planned_vram_gb": None,
    }


def render_preset(
    entries: list[ContextPlan],
    *,
    global_options: Optional[Mapping[str, Any]] = None,
    per_entry_options: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> str:
    """Render a llama.cpp Router Mode preset using hyphenated option names."""
    lines = [
        "; models_preset.ini - generated by a0_lmm_router context planner",
        "; Role ctx values are minimums; ctx-size below is the planned hard context.",
        "; Per-model sections may override [*] defaults (from model_params_cache).",
        "",
    ]
    global_options = dict(global_options or {})
    per_entry_options = dict(per_entry_options or {})
    if global_options:
        lines.append("[*]")
        for key, value in global_options.items():
            if value is not None and value != "":
                lines.append(f"{key} = {value}")
        lines.append("")
    for entry in entries:
        lines.extend(
            [
                f"[{entry.alias}]",
                f"alias = {entry.alias}",
                f"model = {entry.model_path}",
                f"ctx-size = {entry.hard_ctx}",
            ]
        )
        overrides = per_entry_options.get(entry.alias) or {}
        for key, value in overrides.items():
            if key in ("alias", "model", "ctx-size", "embedding"):
                continue
            if value is not None and value != "":
                lines.append(f"{key} = {value}")
        if entry.role == "embed":
            lines.append("embedding = true")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def container_path_to_host(container_path: str, models_dir: str) -> str:
    """Map /models/foo.gguf to the host models directory for metadata reads."""
    path = (container_path or "").strip()
    if not path.startswith("/models/"):
        return path
    root = (models_dir or "").strip()
    if not root:
        return path
    return str(Path(root) / path[len("/models/") :])
