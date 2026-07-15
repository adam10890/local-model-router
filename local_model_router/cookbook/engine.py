"""Cookbook engine: scan a models directory, model VRAM fit, rank per role.

Fit model (whichllm-style, simplified for GGUF + llama.cpp):

    needed = weights (file size, fully offloaded)
           + KV cache (kv_bytes_per_token * context)
           + runtime overhead (~0.8 GiB)

    budget = GPU VRAM - safety margin (from context_policy)

Every number in the report carries reasons and a confidence grade:
``high`` when GGUF metadata was parsed, ``medium`` when estimated from
file size / filename, ``low`` when the file could not be read.

The engine is pure: callers pass hardware and policy as plain dicts
(read from the fleet YAML by the service layer). No HTTP, no config I/O.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .gguf import (
    GgufMeta,
    estimate_params,
    quant_from_filename,
    read_gguf_meta,
    shard_base,
    shard_info,
)

GIB = 1024 ** 3
OVERHEAD_BYTES = int(0.8 * GIB)  # CUDA context + compute buffers, rough
# fallback when geometry is unknown: ~0.125 MiB/token (8B-class, GQA, f16)
FALLBACK_KV_PER_TOKEN = 131072

ROLES = ("chat", "utility", "embed", "scribe")

# folder-name → role hint (matches the models-dir convention: role subdirs)
_FOLDER_ROLES = {
    "chat": "chat",
    "utility": "utility",
    "subagents": "utility",
    "embedding": "embed",
    "embeddings": "embed",
    "embed": "embed",
    "scribe": "scribe",
}

_EMBED_NAME_HINTS = ("embed", "bge-", "nomic", "minilm", "gte-")


def assess_catalog_model(
    model: Dict[str, Any],
    hardware: Dict[str, Any],
    backend: str,
) -> Dict[str, Any]:
    """Use the Cookbook memory rules for a first-run catalog entry."""
    gpus = (hardware or {}).get("gpus") or []
    vram_mb = max(
        [
            int(gpu.get("dedicated_vram_mb") or gpu.get("total_vram_mb") or 0)
            for gpu in gpus
            if isinstance(gpu, dict)
        ]
        or [0]
    )
    ram = (hardware or {}).get("ram") or {}
    ram_gb = float(ram.get("total_mb") or 0) / 1024
    size_gb = float(model.get("size_bytes") or 0) / GIB or float(model.get("size_gb") or 0)
    kv_gb = float(model.get("estimated_kv_cache_gb") or 0)
    gpu_required = max(float(model.get("min_vram_gb") or 0), size_gb + kv_gb + 0.8)
    ram_required = max(float(model.get("min_ram_gb") or 0), size_gb + kv_gb + 3.0)
    gpu_backend = backend not in {"cpu", "existing"}
    if gpu_backend and vram_mb and vram_mb / 1024 >= gpu_required:
        fit = "full_gpu"
        reason = f"Fits {vram_mb / 1024:.1f} GB detected VRAM on {backend}"
    elif ram_gb >= ram_required:
        fit = "cpu" if backend == "cpu" else "partial_offload"
        reason = f"Fits {ram_gb:.1f} GB detected RAM with CPU offload"
    else:
        fit = "incompatible"
        reason = f"Needs about {ram_required:.1f} GB RAM or {gpu_required:.1f} GB VRAM"
    return {
        "fit": fit,
        "fit_reason": reason,
        "estimated_runtime_memory_gb": round(gpu_required if gpu_backend else ram_required, 2),
        "fit_confidence": "high" if ram_gb and (vram_mb or backend == "cpu") else "medium" if ram_gb else "low",
    }


@dataclass
class ModelReport:
    """Fit assessment for one GGUF model."""

    file: str
    path: str
    role_hint: Optional[str]
    size_bytes: int
    quant: str
    params: Optional[int]
    n_ctx_train: Optional[int]
    kv_per_token: Optional[int]
    fit: str  # full_gpu | partial_offload | too_big | unknown
    max_ctx_fit: int
    confidence: str  # high | medium | low
    reasons: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "path": self.path,
            "role_hint": self.role_hint,
            "size_gb": round(self.size_bytes / GIB, 2),
            "quant": self.quant or None,
            "params_b": round(self.params / 1e9, 2) if self.params else None,
            "n_ctx_train": self.n_ctx_train,
            "kv_mib_per_token": (
                round(self.kv_per_token / (1024 ** 2), 3) if self.kv_per_token else None
            ),
            "fit": self.fit,
            "max_ctx_fit": self.max_ctx_fit,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "scores": {k: round(v, 1) for k, v in self.scores.items()},
        }


def _role_hint_for(path: Path, root: Path) -> Optional[str]:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    for part in relative.parts[:-1]:
        hint = _FOLDER_ROLES.get(part.lower())
        if hint:
            return hint
    lower = path.name.lower()
    if any(token in lower for token in _EMBED_NAME_HINTS):
        return "embed"
    return None


def scan_models_dir(models_dir: str) -> List[Dict[str, Any]]:
    """Find GGUF files under models_dir. Multi-part shards collapse to one
    entry (sizes summed, metadata from the first shard)."""
    root = Path(models_dir)
    if not root.is_dir():
        return []

    groups: Dict[str, Dict[str, Any]] = {}
    for path in sorted(root.rglob("*.gguf")):
        if not path.is_file():
            continue
        shard = shard_info(path.name)
        if shard:
            index, _total = shard
            key = str(path.parent / shard_base(path.name))
            group = groups.setdefault(
                key, {"path": None, "size_bytes": 0, "role_hint": _role_hint_for(path, root)}
            )
            group["size_bytes"] += path.stat().st_size
            if index == 1:
                group["path"] = path
        else:
            groups[str(path)] = {
                "path": path,
                "size_bytes": path.stat().st_size,
                "role_hint": _role_hint_for(path, root),
            }

    return [g for g in groups.values() if g["path"] is not None]


def _assess(entry: Dict[str, Any], vram_budget_bytes: int, ram_avail_bytes: int) -> ModelReport:
    path: Path = entry["path"]
    size: int = entry["size_bytes"]
    reasons: List[str] = []

    meta: Optional[GgufMeta] = None
    try:
        meta = read_gguf_meta(path)
    except (ValueError, OSError) as exc:
        reasons.append(f"metadata unreadable ({exc}); size-only estimate")

    quant = (meta.quant if meta else "") or quant_from_filename(path.name)
    if meta and meta.parameter_count:
        params: Optional[int] = meta.parameter_count
        confidence = "high"
        reasons.append(f"params from GGUF metadata: {params / 1e9:.1f}B")
    elif quant:
        params = estimate_params(size, quant)
        confidence = "medium" if meta else "low"
        reasons.append(f"params estimated from size at {quant}: ~{params / 1e9:.1f}B")
    else:
        params = estimate_params(size, "")
        confidence = "low"
        reasons.append(f"params guessed from size alone: ~{params / 1e9:.1f}B")

    kv_per_token = meta.kv_bytes_per_token() if meta else None
    if kv_per_token:
        reasons.append(
            f"KV cache {kv_per_token / (1024 ** 2):.3f} MiB/token "
            f"({meta.n_layers} layers, {meta.n_head_kv or meta.n_head} KV heads)"
        )
    else:
        kv_per_token = FALLBACK_KV_PER_TOKEN
        reasons.append("KV cache geometry unknown; assuming 0.125 MiB/token")
        if confidence == "high":
            confidence = "medium"

    free_for_ctx = vram_budget_bytes - size - OVERHEAD_BYTES
    if free_for_ctx > 0:
        max_ctx = int(free_for_ctx // kv_per_token)
        max_ctx = (max_ctx // 1024) * 1024  # round down to a sane boundary
        if meta and meta.n_ctx_train:
            max_ctx = min(max_ctx, meta.n_ctx_train)
        fit = "full_gpu" if max_ctx >= 2048 else "partial_offload"
        if fit == "full_gpu":
            reasons.append(
                f"fits on GPU with up to {max_ctx} ctx inside "
                f"{vram_budget_bytes / GIB:.1f} GiB budget"
            )
        else:
            reasons.append("weights fit but context headroom is below 2048 tokens")
    elif size <= vram_budget_bytes + ram_avail_bytes:
        max_ctx = 0
        fit = "partial_offload"
        reasons.append("weights exceed VRAM budget; would need CPU offload (slow)")
    else:
        max_ctx = 0
        fit = "too_big"
        reasons.append("exceeds VRAM + available RAM; not runnable on this machine")

    if entry.get("role_hint"):
        reasons.append(f"role hint from folder: {entry['role_hint']}")

    return ModelReport(
        file=path.name,
        path=str(path),
        role_hint=entry.get("role_hint"),
        size_bytes=size,
        quant=quant,
        params=params,
        n_ctx_train=meta.n_ctx_train if meta else None,
        kv_per_token=kv_per_token,
        fit=fit,
        max_ctx_fit=max_ctx,
        confidence=confidence,
        reasons=reasons,
    )


def _quality(params: Optional[int]) -> float:
    """log2-scaled size as a knowledge proxy (whichllm-style), 0-100."""
    if not params or params <= 0:
        return 30.0
    return max(0.0, min(100.0, 18.0 * math.log2(params / 1e9 + 0.25) + 45.0))


def _speed(size_bytes: int) -> float:
    """Inverse-size speed proxy, 0-100 (smaller weights stream faster)."""
    return max(0.0, min(100.0, 100.0 - (size_bytes / GIB) * 4.0))


def _score_roles(report: ModelReport, role_min_ctx: Dict[str, int]) -> None:
    fit_factor = {"full_gpu": 1.0, "partial_offload": 0.45, "too_big": 0.0, "unknown": 0.3}
    factor = fit_factor.get(report.fit, 0.3)
    quality = _quality(report.params)
    speed = _speed(report.size_bytes)

    for role in ROLES:
        if report.role_hint and report.role_hint != role:
            continue  # an explicit folder assignment pins the model to its role
        if not report.role_hint and role == "embed":
            continue  # never recommend an unlabeled model as an embedder
        min_ctx = int(role_min_ctx.get(role, 4096) or 4096)
        ctx_ok = 1.0 if report.max_ctx_fit >= min_ctx else 0.6
        if role == "chat":
            base = 0.75 * quality + 0.25 * min(100.0, report.max_ctx_fit / 1024)
        elif role in ("utility", "scribe"):
            base = 0.55 * speed + 0.45 * quality
        else:  # embed
            base = 60.0
        report.scores[role] = base * factor * ctx_ok


def build_report(
    models_dir: str,
    hardware: Dict[str, Any],
    context_policy: Dict[str, Any],
) -> Dict[str, Any]:
    """Full cookbook report: hardware budget, per-model fit, per-role picks."""
    gpus = (hardware or {}).get("gpus") or []
    gpu = next((g for g in gpus if g.get("enabled", True)), None) or {}
    vram_gb = float(gpu.get("vram_gb", 0) or 0)
    margin_gb = float((context_policy or {}).get("vram_safety_margin_gb", 2.5) or 2.5)
    ram = (hardware or {}).get("ram") or {}
    ram_avail_gb = float(ram.get("available_gb", 0) or 0)
    role_min_ctx = (context_policy or {}).get("role_min_ctx") or {}

    vram_budget = int(max(0.0, vram_gb - margin_gb) * GIB)
    entries = scan_models_dir(models_dir)
    reports = [_assess(entry, vram_budget, int(ram_avail_gb * GIB)) for entry in entries]
    for report in reports:
        _score_roles(report, role_min_ctx)
    reports.sort(key=lambda r: -max(r.scores.values(), default=0.0))

    recommendations: Dict[str, Any] = {}
    for role in ROLES:
        candidates = [r for r in reports if role in r.scores and r.fit != "too_big"]
        if not candidates:
            continue
        best = max(candidates, key=lambda r: r.scores[role])
        min_ctx = int(role_min_ctx.get(role, 0) or 0)
        warnings = []
        if min_ctx and best.max_ctx_fit < min_ctx:
            warnings.append(
                f"best candidate fits only {best.max_ctx_fit} ctx; "
                f"role policy asks for {min_ctx}"
            )
        recommendations[role] = {
            "file": best.file,
            "score": round(best.scores[role], 1),
            "confidence": best.confidence,
            "warnings": warnings,
        }

    return {
        "generated_at": int(time.time()),
        "models_dir": models_dir,
        "hardware": {
            "gpu": gpu.get("name") or None,
            "vram_gb": vram_gb,
            "vram_budget_gb": round(vram_budget / GIB, 1),
            "safety_margin_gb": margin_gb,
            "ram_available_gb": ram_avail_gb,
        },
        "model_count": len(reports),
        "models": [r.to_dict() for r in reports],
        "recommendations": recommendations,
    }
