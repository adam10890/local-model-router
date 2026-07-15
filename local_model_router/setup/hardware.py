"""Best-effort, evidence-labelled hardware discovery for first-run setup."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from local_model_router.helpers.compute_monitor import scan_hardware


def _command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def _platform_id() -> tuple[str, str, str]:
    raw_os = (platform.system() or "unknown").lower()
    os_id = {"darwin": "macos", "windows": "windows", "linux": "linux"}.get(raw_os, "unknown")
    raw_arch = (platform.machine() or "unknown").lower()
    arch = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(raw_arch, raw_arch or "unknown")
    release = (platform.release() or "").lower()
    environment = (
        "wsl"
        if os_id == "linux" and (os.environ.get("WSL_DISTRO_NAME") or "microsoft" in release)
        else "container"
        if os.environ.get("container") or Path("/.dockerenv").exists()
        else "native"
    )
    return os_id, arch, environment


def _vulkan_devices(run: Callable[..., Any] = subprocess.run) -> list[str]:
    """Return actual Vulkan device names; an installed command alone is not evidence."""
    if not _command_exists("vulkaninfo"):
        return []
    try:
        result = run(
            ["vulkaninfo", "--summary"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            shell=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    devices = []
    for line in result.stdout.splitlines():
        if "deviceName" not in line or "=" not in line:
            continue
        name = line.split("=", 1)[1].strip()
        if name and name not in devices:
            devices.append(name)
    return devices


def _nvidia_driver(run: Callable[..., Any] = subprocess.run) -> str | None:
    try:
        result = run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return result.stdout.splitlines()[0].strip() if result.returncode == 0 and result.stdout.strip() else None


def _windows_adapters(
    run: Callable[..., Any] = subprocess.run,
) -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    command = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Json -Compress"
    )
    try:
        result = run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            shell=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    rows = payload if isinstance(payload, list) else [payload]
    adapters = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not str(row.get("Name") or "").strip():
            continue
        raw_ram = row.get("AdapterRAM")
        ram_mb = int(raw_ram // (1024 * 1024)) if isinstance(raw_ram, int) and raw_ram > 0 else None
        adapters.append(
            {
                "id": index,
                "name": str(row["Name"]).strip(),
                "reported_graphics_memory_mb": ram_mb,
                "driver_version": str(row.get("DriverVersion") or "").strip() or None,
                "source": "windows_cim",
                "confidence": "medium" if ram_mb else "low",
            }
        )
    return adapters


def collect_hardware_profile(
    *,
    target_dir: str | Path | None = None,
    adapter_query: Callable[[], list[dict[str, Any]]] = _windows_adapters,
    vulkan_query: Callable[[], list[str]] = _vulkan_devices,
) -> dict[str, Any]:
    """Return a versioned setup profile without treating unknown VRAM as zero."""
    snapshot = scan_hardware()
    compute = snapshot.to_dict()
    nvidia_gpus = compute.get("gpus") or []
    adapters = adapter_query()
    nvidia_driver = _nvidia_driver() if nvidia_gpus else None

    if nvidia_gpus:
        gpus = [
            {
                **gpu,
                "dedicated_vram_mb": gpu.get("total_vram_mb"),
                "source": "nvidia-smi",
                "confidence": "high",
                "driver_version": nvidia_driver,
            }
            for gpu in nvidia_gpus
        ]
        known_names = {str(gpu.get("name") or "").strip().lower() for gpu in gpus}
        gpus.extend(
            adapter
            for adapter in adapters
            if str(adapter.get("name") or "").strip().lower() not in known_names
        )
    else:
        gpus = adapters

    destination = Path(target_dir or Path.cwd()).expanduser().resolve(strict=False)
    try:
        usage = shutil.disk_usage(destination if destination.exists() else destination.parent)
        disk = {
            "path": str(destination),
            "total_gb": round(usage.total / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
            "source": "shutil.disk_usage",
            "confidence": "high",
        }
    except OSError:
        disk = {
            "path": str(destination),
            "total_gb": None,
            "free_gb": None,
            "source": "unavailable",
            "confidence": "low",
        }

    os_id, arch, environment = _platform_id()
    vulkan_devices = vulkan_query() if os_id in {"windows", "linux"} else []
    names = " ".join(str(gpu.get("name") or "") for gpu in gpus).lower()
    vendor_vulkan = any(vendor in names for vendor in ("amd", "radeon", "intel", "arc", "nvidia"))
    capabilities = {
        "cuda": os_id in {"windows", "linux"} and bool(nvidia_gpus),
        "vulkan": os_id in {"windows", "linux"} and bool(vulkan_devices or vendor_vulkan),
        "hip": os_id in {"windows", "linux"} and (_command_exists("rocminfo") or _command_exists("rocm-smi")),
        "sycl": os_id in {"windows", "linux"} and _command_exists("sycl-ls"),
        "metal": os_id == "macos",
    }
    preferred_backend = (
        "cuda12"
        if capabilities["cuda"]
        else "metal"
        if capabilities["metal"]
        else "vulkan"
        if capabilities["vulkan"]
        else "cpu"
    )
    accelerator_evidence = {
        "cuda": {
            "available": capabilities["cuda"],
            "source": "nvidia-smi" if nvidia_gpus else "unavailable",
            "confidence": "high" if nvidia_gpus else "low",
        },
        "vulkan": {
            "available": capabilities["vulkan"],
            "source": "vulkaninfo_device" if vulkan_devices else "adapter_vendor_inference" if vendor_vulkan else "unavailable",
            "confidence": "high" if vulkan_devices else "medium" if capabilities["vulkan"] else "low",
            "devices": vulkan_devices,
        },
        "hip": {
            "available": capabilities["hip"],
            "source": "rocm_tools" if capabilities["hip"] else "unavailable",
            "confidence": "high" if capabilities["hip"] else "low",
        },
        "sycl": {
            "available": capabilities["sycl"],
            "source": "sycl-ls" if capabilities["sycl"] else "unavailable",
            "confidence": "high" if capabilities["sycl"] else "low",
        },
        "metal": {
            "available": capabilities["metal"],
            "source": "macos_platform" if capabilities["metal"] else "unavailable",
            "confidence": "medium" if capabilities["metal"] else "low",
        },
    }

    ram = compute.get("ram") or {}
    return {
        "schema_version": 2,
        "scan_complete": True,
        "platform": {
            "os": os_id,
            "arch": arch,
            "environment": environment,
            "key": f"{os_id}-{arch}",
            "source": "python_platform",
            "confidence": "high" if os_id != "unknown" and arch != "unknown" else "low",
        },
        "system": {
            "os": platform.system() or "unknown",
            "release": platform.release() or "unknown",
            "machine": platform.machine() or "unknown",
            "processor": platform.processor() or "unknown",
            "source": "python_platform",
            "confidence": "high",
        },
        "cpu": {
            **(compute.get("cpu") or {}),
            "logical_cores": os.cpu_count(),
            "source": "psutil_python",
            "confidence": "high",
        },
        "ram": {
            **ram,
            "source": "psutil",
            "confidence": "high",
        },
        "gpus": gpus,
        "accelerators": capabilities,
        "accelerator_evidence": accelerator_evidence,
        "preferred_backend": preferred_backend,
        "preferred_backend_evidence": {
            "source": accelerator_evidence["cuda" if preferred_backend == "cuda12" else preferred_backend]["source"]
            if preferred_backend != "cpu"
            else "cpu_fallback",
            "confidence": accelerator_evidence["cuda" if preferred_backend == "cuda12" else preferred_backend]["confidence"]
            if preferred_backend != "cpu"
            else "high",
        },
        "disk": disk,
    }
