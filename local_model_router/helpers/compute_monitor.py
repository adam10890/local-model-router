"""
Compute Monitor — real-time GPU/CPU/RAM stats for the LMM Router fleet.

Wraps nvidia-smi (or fallback) and merges with BackendManager slot data
so the dashboard can display a single unified view.

Pipeline:
    scan_hardware() -> compute_vram_budget() -> snapshot() -> ComputeSnapshot

The ComputeSnapshot is consumed by the fleet_status API.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GPUStats:
    id: int
    name: str
    total_vram_mb: int
    used_vram_mb: int
    free_vram_mb: int
    utilization_pct: int
    temperature_c: int

@dataclass
class CPUStats:
    load_pct: float
    ram_total_mb: int
    ram_used_mb: int
    ram_free_mb: int

@dataclass
class SlotInfo:
    id: str
    role: str
    model_id: str
    port: Optional[int]
    running: bool
    healthy: bool
    router_mode: bool = False
    router_models_dir: str = ""
    router_models_preset: str = ""
    router_models_max: int = 1
    router_models_autoload: bool = True
    registered_models: Optional[List[str]] = None
    source: str = "config"

@dataclass
class ComputeSnapshot:
    ts: float
    gpus: List[GPUStats]
    cpu: CPUStats
    slots: List[SlotInfo]
    vram_budget_mb: int = 0
    vram_used_mb: int = 0
    vram_free_mb: int = 0
    vram_utilization_pct: float = 0.0
    ram_total_mb: int = 0
    ram_used_mb: int = 0
    ram_free_mb: int = 0
    ram_utilization_pct: float = 0.0


# ---------------------------------------------------------------------------
# GPU helpers (nvidia-smi)
# ---------------------------------------------------------------------------

def _query_gpus_local() -> List[GPUStats]:
    """Try `nvidia-smi` in the current process. Empty list if unavailable."""
    try:
        cmd = [
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            logger.debug("nvidia-smi returned non-zero: %s", result.stderr.strip())
            return []
        gpus: List[GPUStats] = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                gpus.append(GPUStats(
                    id=int(parts[0]),
                    name=parts[1],
                    total_vram_mb=int(parts[2]),
                    used_vram_mb=int(parts[3]),
                    free_vram_mb=int(parts[4]),
                    utilization_pct=int(parts[5]),
                    temperature_c=int(parts[6]),
                ))
        return gpus
    except FileNotFoundError:
        logger.debug("nvidia-smi not found locally")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("nvidia-smi timed out")
        return []
    except Exception as exc:
        logger.warning("Local GPU query failed: %s", exc)
        return []


def _query_cpu() -> CPUStats:
    """Return basic CPU/RAM stats using cross-platform approach."""
    load_pct = 0.0
    ram_total = 0
    ram_used = 0
    ram_free = 0

    try:
        import psutil  # type: ignore
        mem = psutil.virtual_memory()
        ram_total = mem.total // (1024 * 1024)
        ram_used = mem.used // (1024 * 1024)
        ram_free = mem.available // (1024 * 1024)
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("psutil failed: %s", exc)

    if ram_total == 0:
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        ram_total = int(line.split()[1]) // (1024 * 1024)
                        break
                    if line.startswith("MemFree:"):
                        ram_free = int(line.split()[1]) // (1024 * 1024)
                        break
                    if line.startswith("MemAvailable:"):
                        ram_free = int(line.split()[1]) // (1024 * 1024)
                        break
        except Exception:
            pass

    if ram_total == 0:
        ram_total = 1
        ram_used = 0
        ram_free = 0

    ram_used = max(0, ram_total - ram_free)

    return CPUStats(
        load_pct=load_pct,
        ram_total_mb=ram_total,
        ram_used_mb=ram_used,
        ram_free_mb=ram_free,
    )


# ---------------------------------------------------------------------------
# Full scan
# ---------------------------------------------------------------------------

def scan_hardware() -> ComputeSnapshot:
    """
    Full hardware scan: GPU + CPU/RAM + active slots.

    Returns a ComputeSnapshot that the fleet_status API can serve.
    """
    ts = os.currentTimeMillis()

    # GPU
    gpus = _query_gpus_local()

    # CPU/RAM
    cpu = _query_cpu()

    # Slots (from observer)
    slots: List[SlotInfo] = []
    try:
        from local_model_router.service.observer import ObserverBackend

        observer = ObserverBackend()
        slot_list = observer.get_slots()
        for slot in slot_list:
            slots.append(SlotInfo(
                id=str(slot.get("id", "")),
                role=str(slot.get("role", "chat")),
                model_id=str(slot.get("model_id", "")),
                port=int(slot.get("port", 8080)),
                running=bool(slot.get("running", False)),
                healthy=bool(slot.get("healthy", False)),
                router_mode=bool(slot.get("router_mode", False)),
                router_models_dir=str(slot.get("router_models_dir", "")),
                router_models_preset=str(slot.get("router_models_preset", "")),
                router_models_max=int(slot.get("router_models_max", 1)),
                router_models_autoload=bool(slot.get("router_models_autoload", True)),
            ))
    except Exception as exc:
        logger.warning("Could not query slots: %s", exc)

    # VRAM budget (from GPU total)
    vram_total_mb = sum(g.total_vram_mb for g in gpus)
    vram_used_mb = sum(g.used_vram_mb for g in gpus)
    vram_free_mb = vram_total_mb - vram_used_mb
    vram_utilization_pct = (vram_used_mb / vram_total_mb * 100) if vram_total_mb > 0 else 0.0

    # RAM
    ram_total_mb = cpu.ram_total_mb
    ram_used_mb = cpu.ram_used_mb
    ram_free_mb = cpu.ram_free_mb
    ram_utilization_pct = (ram_used_mb / ram_total_mb * 100) if ram_total_mb > 0 else 0.0

    return ComputeSnapshot(
        ts=ts,
        gpus=gpus,
        cpu=cpu,
        slots=slots,
        vram_budget_mb=vram_total_mb,
        vram_used_mb=vram_used_mb,
        vram_free_mb=vram_free_mb,
        vram_utilization_pct=round(vram_utilization_pct, 1),
        ram_total_mb=ram_total_mb,
        ram_used_mb=ram_used_mb,
        ram_free_mb=ram_free_mb,
        ram_utilization_pct=round(ram_utilization_pct, 1),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def snapshot() -> ComputeSnapshot:
    """Quick snapshot of current compute resources."""
    return scan_hardware()
