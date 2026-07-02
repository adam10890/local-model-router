"""Best-effort local GPU, CPU, and RAM telemetry."""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

import psutil

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024
_NVIDIA_SMI_COMMAND = (
    "nvidia-smi",
    "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu",
    "--format=csv,noheader,nounits",
)


@dataclass(frozen=True)
class GPUStats:
    id: int
    name: str
    total_vram_mb: int
    used_vram_mb: int
    free_vram_mb: int
    utilization_pct: float
    temperature_c: float | None


@dataclass(frozen=True)
class CPUStats:
    utilization_pct: float
    ram_total_mb: int
    ram_used_mb: int
    ram_available_mb: int


@dataclass(frozen=True)
class ComputeSnapshot:
    timestamp: float
    gpus: tuple[GPUStats, ...]
    cpu: CPUStats

    def to_dict(self) -> dict[str, Any]:
        ram_utilization = (
            self.cpu.ram_used_mb / self.cpu.ram_total_mb * 100
            if self.cpu.ram_total_mb
            else 0.0
        )
        return {
            "available": True,
            "timestamp": self.timestamp,
            "gpus": [asdict(gpu) for gpu in self.gpus],
            "cpu": {"utilization_pct": self.cpu.utilization_pct},
            "ram": {
                "total_mb": self.cpu.ram_total_mb,
                "used_mb": self.cpu.ram_used_mb,
                "available_mb": self.cpu.ram_available_mb,
                "utilization_pct": round(ram_utilization, 1),
            },
        }

    def vram_summary(self) -> dict[str, float | str | None]:
        if not self.gpus:
            return {
                "total_gb": None,
                "used_gb": None,
                "available_gb": None,
                "source": "nvidia-smi_unavailable",
            }

        total_mb = sum(gpu.total_vram_mb for gpu in self.gpus)
        used_mb = sum(gpu.used_vram_mb for gpu in self.gpus)
        available_mb = sum(gpu.free_vram_mb for gpu in self.gpus)
        return {
            "total_gb": round(total_mb / 1024, 2),
            "used_gb": round(used_mb / 1024, 2),
            "available_gb": round(available_mb / 1024, 2),
            "source": "nvidia-smi",
        }


def _optional_float(value: str) -> float | None:
    return None if value.upper() == "N/A" else float(value)


def query_gpus(
    *,
    run: Callable[..., Any] = subprocess.run,
) -> tuple[GPUStats, ...]:
    """Return NVIDIA GPU data, or an empty tuple when it is unavailable."""
    try:
        result = run(
            _NVIDIA_SMI_COMMAND,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("nvidia-smi unavailable: %s", exc)
        return ()

    if result.returncode != 0:
        logger.debug("nvidia-smi failed: %s", result.stderr.strip())
        return ()

    gpus = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 7:
            logger.warning("Ignoring malformed nvidia-smi row")
            continue
        try:
            gpus.append(
                GPUStats(
                    id=int(parts[0]),
                    name=parts[1],
                    total_vram_mb=int(float(parts[2])),
                    used_vram_mb=int(float(parts[3])),
                    free_vram_mb=int(float(parts[4])),
                    utilization_pct=float(parts[5]),
                    temperature_c=_optional_float(parts[6]),
                )
            )
        except ValueError:
            logger.warning("Ignoring invalid nvidia-smi values")
    return tuple(gpus)


def query_cpu(*, psutil_module: Any = psutil) -> CPUStats:
    """Return cross-platform CPU and RAM data from psutil."""
    memory = psutil_module.virtual_memory()
    total_mb = int(memory.total // _MIB)
    available_mb = int(memory.available // _MIB)
    return CPUStats(
        utilization_pct=round(float(psutil_module.cpu_percent(interval=0.1)), 1),
        ram_total_mb=total_mb,
        ram_used_mb=max(0, total_mb - available_mb),
        ram_available_mb=available_mb,
    )


def scan_hardware(
    *,
    gpu_query: Callable[[], tuple[GPUStats, ...]] = query_gpus,
    cpu_query: Callable[[], CPUStats] = query_cpu,
    clock: Callable[[], float] = time.time,
) -> ComputeSnapshot:
    """Collect one local hardware snapshot."""
    return ComputeSnapshot(
        timestamp=clock(),
        gpus=gpu_query(),
        cpu=cpu_query(),
    )
