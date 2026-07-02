"""Hermetic tests for local hardware telemetry."""
from __future__ import annotations

from types import SimpleNamespace

from local_model_router.helpers import compute_monitor as monitor


MIB = 1024 * 1024


def test_query_gpus_parses_valid_rows_and_skips_malformed_rows():
    result = SimpleNamespace(
        returncode=0,
        stdout=(
            "0, NVIDIA GeForce RTX 4090, 24576, 6144, 18432, 37, 49\n"
            "malformed row\n"
            "1, NVIDIA L4, 23034, 1024, 22010, 5, N/A\n"
        ),
        stderr="",
    )
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    gpus = monitor.query_gpus(run=fake_run)

    assert gpus == (
        monitor.GPUStats(0, "NVIDIA GeForce RTX 4090", 24576, 6144, 18432, 37.0, 49.0),
        monitor.GPUStats(1, "NVIDIA L4", 23034, 1024, 22010, 5.0, None),
    )
    assert calls[0][1]["timeout"] == 5
    assert calls[0][1]["shell"] is False


def test_query_gpus_returns_empty_when_nvidia_smi_is_unavailable():
    def missing(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    assert monitor.query_gpus(run=missing) == ()


def test_query_cpu_reports_cross_platform_utilization_and_available_ram():
    intervals = []

    def cpu_percent(interval=None):
        intervals.append(interval)
        return 37.5

    fake_psutil = SimpleNamespace(
        cpu_percent=cpu_percent,
        virtual_memory=lambda: SimpleNamespace(
            total=16 * MIB,
            available=6 * MIB,
        ),
    )

    cpu = monitor.query_cpu(psutil_module=fake_psutil)

    assert cpu == monitor.CPUStats(
        utilization_pct=37.5,
        ram_total_mb=16,
        ram_used_mb=10,
        ram_available_mb=6,
    )
    assert intervals == [0.1]


def test_scan_hardware_serializes_compute_and_backward_compatible_vram():
    gpu = monitor.GPUStats(
        id=0,
        name="NVIDIA GeForce RTX 4090",
        total_vram_mb=24576,
        used_vram_mb=6144,
        free_vram_mb=18432,
        utilization_pct=37.0,
        temperature_c=49.0,
    )
    cpu = monitor.CPUStats(
        utilization_pct=25.0,
        ram_total_mb=32768,
        ram_used_mb=12288,
        ram_available_mb=20480,
    )

    snapshot = monitor.scan_hardware(
        gpu_query=lambda: (gpu,),
        cpu_query=lambda: cpu,
        clock=lambda: 1_750_000_000.25,
    )

    assert snapshot.to_dict() == {
        "available": True,
        "timestamp": 1_750_000_000.25,
        "gpus": [
            {
                "id": 0,
                "name": "NVIDIA GeForce RTX 4090",
                "total_vram_mb": 24576,
                "used_vram_mb": 6144,
                "free_vram_mb": 18432,
                "utilization_pct": 37.0,
                "temperature_c": 49.0,
            }
        ],
        "cpu": {"utilization_pct": 25.0},
        "ram": {
            "total_mb": 32768,
            "used_mb": 12288,
            "available_mb": 20480,
            "utilization_pct": 37.5,
        },
    }
    assert snapshot.vram_summary() == {
        "total_gb": 24.0,
        "used_gb": 6.0,
        "available_gb": 18.0,
        "source": "nvidia-smi",
    }


def test_vram_summary_is_explicit_when_no_nvidia_gpu_is_visible():
    snapshot = monitor.ComputeSnapshot(
        timestamp=1_750_000_000.25,
        gpus=(),
        cpu=monitor.CPUStats(0.0, 1024, 512, 512),
    )

    assert snapshot.vram_summary() == {
        "total_gb": None,
        "used_gb": None,
        "available_gb": None,
        "source": "nvidia-smi_unavailable",
    }
