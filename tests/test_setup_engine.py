from __future__ import annotations

import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from local_model_router.service.readiness import build_ui_status
from local_model_router.setup import SetupEngine, SetupError
from local_model_router.setup import engine as engine_module
from local_model_router.setup import hardware as hardware_module
from local_model_router.helpers.backends import subprocess_backend as subprocess_backend_module


def _hardware() -> dict:
    return {
        "schema_version": 2,
        "scan_complete": True,
        "platform": {"os": "windows", "arch": "x86_64", "environment": "native", "key": "windows-x86_64"},
        "system": {"os": "Windows", "release": "11", "confidence": "high"},
        "cpu": {"logical_cores": 12},
        "ram": {"total_mb": 32768},
        "gpus": [
            {
                "name": "NVIDIA Test GPU",
                "dedicated_vram_mb": 12288,
                "source": "nvidia-smi",
                "confidence": "high",
                "driver_version": "560.00",
            }
        ],
        "accelerators": {"cuda": True, "vulkan": True, "hip": False, "sycl": False},
        "accelerator_evidence": {
            "cuda": {"available": True, "source": "nvidia-smi", "confidence": "high"},
            "vulkan": {"available": True, "source": "vulkaninfo_device", "confidence": "high"},
        },
        "preferred_backend": "cuda12",
        "disk": {"path": "C:\\models", "free_gb": 100, "confidence": "high"},
    }


def _engine(tmp_path: Path) -> SetupEngine:
    engine = SetupEngine(home=tmp_path / "home", config_path=tmp_path / "config.yaml")
    engine._atomic_json(engine.hardware_path, _hardware())
    # Keep setup tests hermetic while production plans deliberately refresh hardware.
    engine.hardware = lambda *, refresh=False: engine._read_json(engine.hardware_path)
    # Mock discover() so backend_candidates uses the mocked hardware, not the real disk.
    engine.discover = lambda: {
        "managed_runtime": None,
        "path_runtime": None,
        "runtime_installed": False,
        "existing_server_available": False,
        "runtime_available": False,
        "models_dir": str(engine.models_dir),
        "gguf_models": [],
        "config_path": str(engine.config_path),
        "config_exists": False,
        "enabled_slots": 0,
        "docker_available": False,
        "ollama_available": False,
        "servers": [],
        "ports": {"8080": False, "11434": False, "1234": False, "12434": False},
        "offline": {"available": False, "directories": [], "assets": []},
    }
    return engine


def _discovery(engine: SetupEngine, *, servers: list[dict] | None = None) -> dict:
    servers = servers or []
    return {
        "managed_runtime": None,
        "path_runtime": None,
        "runtime_installed": False,
        "existing_server_available": bool(servers),
        "runtime_available": bool(servers),
        "models_dir": str(engine.models_dir),
        "gguf_models": [],
        "config_path": str(engine.config_path),
        "config_exists": False,
        "enabled_slots": 0,
        "docker_available": False,
        "ollama_available": False,
        "servers": servers,
        "ports": {"8080": bool(servers)},
        "offline": {"available": False, "directories": [], "assets": []},
    }


def test_recommends_pinned_first_run_model(tmp_path):
    engine = _engine(tmp_path)
    recommendation = engine.recommendation()
    assert recommendation is not None
    assert recommendation["id"] == "qwen3-1.7b-q8"
    assert recommendation["fit"] == "full_gpu"


def test_recommends_strongest_local_model_when_folder_has_ggufs(tmp_path):
    engine = SetupEngine(home=tmp_path / "home", config_path=tmp_path / "config.yaml")
    engine._atomic_json(engine.hardware_path, _hardware())
    engine.hardware = lambda *, refresh=False: engine._read_json(engine.hardware_path)
    models = tmp_path / "my-models"
    models.mkdir()
    (models / "tiny-1.5B-Q4_K_M.gguf").write_bytes(b"small")
    (models / "coder-14B-Q4_K_M.gguf").write_bytes(b"large")
    (models / "nomic-embed-text.gguf").write_bytes(b"embed")
    engine.set_models_dir(str(models))

    recommendation = engine.recommendation()

    assert recommendation is not None
    assert recommendation["id"] == "coder-14B-Q4_K_M"
    assert recommendation["local"] is True
    assert recommendation["parameters_b"] == 14.0
    assert recommendation["source"] == "local_installed"


def test_local_recommendation_skips_models_that_do_not_fit(tmp_path, monkeypatch):
    engine = SetupEngine(home=tmp_path / "home", config_path=tmp_path / "config.yaml")
    hardware = _hardware()
    hardware["gpus"][0]["dedicated_vram_mb"] = 4096
    hardware["ram"]["total_mb"] = 8192
    engine._atomic_json(engine.hardware_path, hardware)
    engine.hardware = lambda *, refresh=False: engine._read_json(engine.hardware_path)
    models = tmp_path / "my-models"
    models.mkdir()
    huge = models / "giant-70B-Q8_0.gguf"
    huge.write_bytes(b"huge")
    small = models / "tiny-1.7B-Q4_K_M.gguf"
    small.write_bytes(b"tiny")
    engine.set_models_dir(str(models))

    real_stat = Path.stat

    def fake_stat(self, *args, **kwargs):
        if self.name == "giant-70B-Q8_0.gguf":
            return type("Stat", (), {"st_size": 80 * 1024**3})()
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    recommendation = engine.recommendation()

    assert recommendation["id"] == "tiny-1.7B-Q4_K_M"
    assert recommendation["local"] is True


def test_plan_is_explicit_and_requires_download_consent(tmp_path):
    engine = _engine(tmp_path)
    plan = engine.plan({"backend": "cuda12", "model_id": "qwen3-1.7b-q8"})
    assert [step["action"] for step in plan["steps"]] == [
        "install_runtime",
        "download_model",
        "write_config",
        "start_runtime",
        "smoke_test",
    ]
    with pytest.raises(SetupError, match="explicit confirmation"):
        engine.apply({"backend": "cuda12", "model_id": "qwen3-1.7b-q8"})


def test_plan_blocks_when_available_ram_is_too_low(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    monkeypatch.setattr(engine, "discover", lambda: _discovery(engine))
    hardware = _hardware()
    hardware["ram"]["available_mb"] = 376
    engine._atomic_json(engine.hardware_path, hardware)

    with pytest.raises(SetupError) as caught:
        engine.plan({"backend": "cuda12", "model_id": "qwen3-1.7b-q8"})

    assert caught.value.payload() == {
        "error": "insufficient_available_memory",
        "detail": (
            "Setup needs about 3.1 GB of available RAM but only 0.4 GB is available. "
            "Close other model servers or applications, then scan again."
        ),
        "remediation": ["close_other_apps", "rescan", "retry", "use_existing_server"],
    }


@pytest.mark.parametrize("available_mb", [4096, None])
def test_plan_accepts_adequate_or_unknown_available_ram(tmp_path, monkeypatch, available_mb):
    engine = _engine(tmp_path)
    monkeypatch.setattr(engine, "discover", lambda: _discovery(engine))
    hardware = _hardware()
    if available_mb is not None:
        hardware["ram"]["available_mb"] = available_mb
    engine._atomic_json(engine.hardware_path, hardware)

    plan = engine.plan({"backend": "cuda12", "model_id": "qwen3-1.7b-q8"})

    assert plan["model"]["id"] == "qwen3-1.7b-q8"
    assert plan["memory_required_gb"] == (pytest.approx(3.085) if available_mb else None)


def test_plan_skips_memory_block_when_selected_model_is_already_served(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    monkeypatch.setattr(
        engine,
        "discover",
        lambda: _discovery(
            engine,
            servers=[
                {
                    "kind": "llama.cpp_or_compatible",
                    "url": "http://127.0.0.1:8080/v1",
                    "source": "models_api",
                    "confidence": "high",
                    "models": ["qwen3-1.7b-q8"],
                }
            ],
        ),
    )
    hardware = _hardware()
    hardware["ram"]["available_mb"] = 376
    engine._atomic_json(engine.hardware_path, hardware)

    plan = engine.plan({"backend": "cuda12", "model_id": "qwen3-1.7b-q8"})

    assert plan["model"]["id"] == "qwen3-1.7b-q8"
    assert plan["memory_required_gb"] is None


def test_plan_accepts_a_valid_managed_port_override(tmp_path):
    engine = _engine(tmp_path)
    plan = engine.plan({"backend": "cuda12", "model_id": "qwen3-1.7b-q8", "port": 18080})
    assert plan["port"] == 18080

    with pytest.raises(SetupError) as caught:
        engine.plan({"backend": "cuda12", "model_id": "qwen3-1.7b-q8", "port": 80})
    assert caught.value.code == "invalid_port"


def test_managed_config_allows_slow_cpu_startup(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    runtime = tmp_path / "llama-server.exe"
    runtime.touch()
    model = next(row for row in engine.catalog["models"] if row["id"] == "qwen3-1.7b-q8")
    engine.models_dir.mkdir(parents=True)
    (engine.models_dir / model["filename"]).touch()
    monkeypatch.setattr(engine, "_available_runtime", lambda: {"binary": str(runtime)})

    config = engine.write_config("qwen3-1.7b-q8", "cpu", managed_port=18080)

    assert config["global"]["startup_timeout"] == 600
    assert config["active_slots"][0]["port"] == 18080
    assert config["active_slots"][0]["router_mode"] is True
    assert config["active_slots"][0]["router_models_dir"] == str(engine.models_dir)
    assert "[qwen3-1.7b-q8]" in Path(config["active_slots"][0]["router_models_preset"]).read_text(encoding="utf-8")
    assert model["runtime_context"] == 4096
    assert config["active_slots"][0]["context_size"] == 4096


def test_simple_chat_smoke_disables_qwen_thinking(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    engine.config_path.write_text(
        "global:\n  backend: remote\nactive_slots:\n"
        "  - id: external\n    host: 127.0.0.1\n    port: 18080\n"
        "    enabled: true\n    model_id: external\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        engine,
        "discover",
        lambda: {
            "runtime_available": False,
            "config_exists": True,
            "gguf_models": [],
        },
    )
    captured = {}

    class Response:
        status = 200

        def __init__(self, payload):
            self.body = json.dumps(payload).encode("utf-8")

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(target, *, timeout):
        url = target if isinstance(target, str) else target.full_url
        if url.endswith("/health"):
            return Response({"status": "ok"})
        if url.endswith("/v1/models"):
            return Response({"data": [{"id": "external"}]})
        captured.update(json.loads(target.data))
        return Response({"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(engine_module.urllib.request, "urlopen", fake_urlopen)

    result = engine.smoke()

    assert result["ok"] is True
    assert captured["max_tokens"] == 4
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}


def test_plan_blocks_download_when_disk_is_too_small(tmp_path):
    engine = _engine(tmp_path)
    hardware = _hardware()
    hardware["disk"]["free_gb"] = 1
    engine._atomic_json(engine.hardware_path, hardware)
    with pytest.raises(SetupError) as caught:
        engine.plan({"backend": "cuda12", "model_id": "qwen3-1.7b-q8"})
    assert caught.value.code == "insufficient_disk_space"


def test_existing_server_plan_skips_llama_and_model_downloads(tmp_path):
    engine = _engine(tmp_path)
    plan = engine.plan({"backend": "existing", "existing_url": "http://127.0.0.1:11434/v1"})
    assert [step["action"] for step in plan["steps"]] == ["write_config", "smoke_test"]
    assert plan["model"] is None
    assert plan["existing_url"].endswith("11434/v1")


def test_latest_runtime_channel_is_preserved_in_plan(tmp_path):
    engine = _engine(tmp_path)
    plan = engine.plan(
        {"backend": "vulkan", "runtime_channel": "latest", "model_id": "qwen3-1.7b-q8"}
    )
    assert plan["runtime_channel"] == "latest"
    assert plan["steps"][0]["channel"] == "latest"


def test_backend_candidates_are_gated_by_detected_hardware(tmp_path):
    engine = _engine(tmp_path)
    hardware = _hardware()
    hardware["accelerators"]["cuda"] = False
    hardware["accelerator_evidence"]["cuda"] = {"available": False, "source": "unavailable", "confidence": "low"}
    engine._atomic_json(engine.hardware_path, hardware)
    rows = {row["id"]: row for row in engine.state()["backend_candidates"]}
    assert rows["cuda12"]["eligible"] is False
    assert rows["vulkan"]["eligible"] is True
    assert engine.state()["recommended_backend"] == "vulkan"


def test_cuda_requires_a_compatible_known_driver(tmp_path):
    engine = _engine(tmp_path)
    hardware = _hardware()
    hardware["gpus"][0]["driver_version"] = "550.00"
    engine._atomic_json(engine.hardware_path, hardware)
    rows = {row["id"]: row for row in engine.state()["backend_candidates"]}
    assert rows["cuda12"]["eligible"] is False
    assert rows["cuda12"]["reason_code"] == "driver_incompatible_or_unknown"
    assert engine.state()["recommended_backend"] == "vulkan"


class _HardwareSnapshot:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def to_dict(self) -> dict:
        return self.payload


def test_windows_cim_memory_is_reported_not_dedicated_vram(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hardware_module,
        "scan_hardware",
        lambda: _HardwareSnapshot({"cpu": {}, "ram": {"total_mb": 8192}, "gpus": []}),
    )
    monkeypatch.setattr(hardware_module, "_platform_id", lambda: ("windows", "x86_64", "native"))
    profile = hardware_module.collect_hardware_profile(
        target_dir=tmp_path,
        adapter_query=lambda: [
            {
                "id": 0,
                "name": "Intel(R) UHD Graphics",
                "reported_graphics_memory_mb": 2048,
                "driver_version": "test",
                "source": "windows_cim",
                "confidence": "medium",
            }
        ],
        vulkan_query=lambda: [],
    )

    assert profile["gpus"][0]["reported_graphics_memory_mb"] == 2048
    assert "dedicated_vram_mb" not in profile["gpus"][0]
    assert profile["accelerator_evidence"]["vulkan"] == {
        "available": True,
        "source": "adapter_vendor_inference",
        "confidence": "medium",
        "devices": [],
    }


def test_vulkaninfo_device_is_verified_hardware_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hardware_module,
        "scan_hardware",
        lambda: _HardwareSnapshot({"cpu": {}, "ram": {"total_mb": 16384}, "gpus": []}),
    )
    monkeypatch.setattr(hardware_module, "_platform_id", lambda: ("windows", "x86_64", "native"))
    profile = hardware_module.collect_hardware_profile(
        target_dir=tmp_path,
        adapter_query=lambda: [],
        vulkan_query=lambda: ["Verified Test GPU"],
    )

    assert profile["accelerator_evidence"]["vulkan"]["source"] == "vulkaninfo_device"
    assert profile["accelerator_evidence"]["vulkan"]["confidence"] == "high"
    assert profile["preferred_backend"] == "vulkan"


def test_verified_offline_runtime_is_preferred_in_closed_environment(tmp_path):
    engine = _engine(tmp_path)
    cpu_asset = next(
        row
        for row in engine.runtime_catalog["platforms"]
        if row["os"] == "windows" and row["arch"] == "x86_64"
    )["backends"]["cpu"]["assets"][0]
    offline = tmp_path / "offline"
    offline.mkdir()
    (offline / cpu_asset["name"]).write_bytes(b"placeholder")
    engine.offline_dirs.insert(0, offline)

    state = engine.state()
    assert state["recommended_backend"] == "cpu"
    plan = engine.plan({"model_id": "qwen3-1.7b-q8"})
    assert plan["steps"][0]["source"] == "offline"


def test_models_directory_is_persisted_and_scans_all_ggufs(tmp_path):
    home = tmp_path / "home"
    models = tmp_path / "my-models"
    (models / "nested").mkdir(parents=True)
    (models / "nested" / "custom.gguf").touch()
    engine = SetupEngine(home=home)

    discovery = engine.set_models_dir(str(models))

    assert discovery["local_models"] == [{
        "id": "nested/custom",
        "name": "custom",
        "path": str(models / "nested" / "custom.gguf"),
    }]
    assert SetupEngine(home=home).models_dir == models.resolve()


def test_setup_uses_an_installed_model_without_downloading_it(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    model = engine.models_dir / "nested" / "custom.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"local-model")
    runtime = tmp_path / "llama-server.exe"
    runtime.touch()
    discovery = _discovery(engine)
    discovery.update({
        "path_runtime": str(runtime),
        "runtime_installed": True,
        "runtime_available": True,
        "gguf_models": [str(model)],
        "local_models": [{"id": "nested/custom", "name": "custom", "path": str(model)}],
    })
    monkeypatch.setattr(engine, "discover", lambda: discovery)
    monkeypatch.setattr(engine, "_available_runtime", lambda: {"binary": str(runtime)})

    plan = engine.plan({"backend": "cpu", "model_id": "nested/custom"})

    assert plan["model"]["path"] == str(model)
    assert all(step["action"] != "download_model" for step in plan["steps"])
    config = engine.write_config("nested/custom", "cpu")
    slot = config["active_slots"][0]
    assert slot["model_id"] == "nested/custom"
    assert slot["model_path"] == str(model)


def test_setup_accepts_a_shallow_system_python_path(tmp_path, monkeypatch):
    shallow_python = Path(Path.cwd().anchor) / "Python312" / "python"
    monkeypatch.setattr(engine_module.sys, "executable", str(shallow_python))

    engine = SetupEngine(home=tmp_path / "home")

    assert engine.offline_dirs[-1] == shallow_python.parent / "offline"


def test_non_windows_platform_is_planned_and_never_offers_cuda(tmp_path):
    engine = _engine(tmp_path)
    hardware = _hardware()
    hardware["platform"] = {"os": "macos", "arch": "arm64", "environment": "native", "key": "macos-arm64"}
    hardware["system"]["os"] = "Darwin"
    hardware["accelerators"] = {"cuda": False, "vulkan": False, "metal": True}
    engine._atomic_json(engine.hardware_path, hardware)
    state = engine.state()
    assert state["platform_support"]["status"] == "planned"
    assert state["recommended_backend"] is None
    assert "cuda12" not in {row["id"] for row in state["backend_candidates"]}
    with pytest.raises(SetupError) as caught:
        engine.plan({})
    assert caught.value.code == "platform_not_supported"


def test_recommended_runtime_plan_uses_pinned_assets_without_release_lookup(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    monkeypatch.setattr(engine, "_github_release", lambda _url: pytest.fail("network lookup was used"))
    plan = engine.plan({"backend": "cpu", "model_id": "qwen3-1.7b-q8"})
    assert plan["runtime_channel"] == "recommended"


def test_runtime_zip_rejects_path_traversal(tmp_path):
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside.exe", "unsafe")
    with zipfile.ZipFile(archive) as bundle:
        with pytest.raises(SetupError) as caught:
            SetupEngine._safe_extract(bundle, tmp_path / "extract")
    assert caught.value.code == "runtime_archive_unsafe"


def test_readiness_returns_stable_next_action_codes():
    payload = build_ui_status(
        setup_state={
            "hardware": _hardware(),
            "discovery": {
                "runtime_installed": True,
                "gguf_models": [],
                "config_exists": False,
                "enabled_slots": 0,
            },
        },
        slots_health=[],
        compute={},
        base_url="http://127.0.0.1:9000",
    )
    assert payload["overall"] == "setup_required"
    assert payload["next_action"]["code"] == "choose_model"
    assert payload["blocking_issues"][0]["code"] == "model_missing"
    assert payload["blocking_issues"][0]["category"] == "configuration"
    assert payload["next_action"]["label"]["he"]


def test_readiness_surfaces_live_memory_pressure_only_before_server_is_healthy():
    setup_state = {
        "hardware": _hardware(),
        "platform_support": {"status": "supported"},
        "recommended_backend": "cpu",
        "recommendation": {
            "id": "qwen3-1.7b-q8",
            "size_gb": 1.71,
            "estimated_kv_cache_gb": 0.375,
        },
        "discovery": {
            "runtime_installed": True,
            "gguf_models": ["Qwen3-1.7B-Q8_0.gguf"],
            "config_exists": True,
            "enabled_slots": 1,
        },
    }
    compute = {"ram": {"total_mb": 7884, "available_mb": 376}}

    pressured = build_ui_status(
        setup_state=setup_state,
        slots_health=[],
        compute=compute,
        base_url="http://127.0.0.1:9000",
    )

    memory_issue = next(
        issue for issue in pressured["blocking_issues"] if issue["code"] == "memory_pressure"
    )
    assert pressured["hardware"]["ram_available_mb"] == 376
    assert memory_issue["category"] == "system"
    assert memory_issue["action"]["code"] == "resolve_memory_pressure"
    assert memory_issue["action"]["href"] == "#/setup/hardware"
    assert pressured["next_action"]["code"] == "free_memory"
    assert pressured["next_action"]["label"]["en"] == "Close apps and scan again"

    healthy = build_ui_status(
        setup_state=setup_state,
        slots_health=[
            {
                "id": "local_default",
                "enabled": True,
                "health": "healthy",
                "model_id": "qwen3-1.7b-q8",
                "backend_type": "subprocess",
            }
        ],
        compute=compute,
        base_url="http://127.0.0.1:9000",
    )

    assert healthy["overall"] == "ready"
    assert healthy["hardware"]["ram_available_mb"] == 376
    assert "memory_pressure" not in {issue["code"] for issue in healthy["blocking_issues"]}
    assert healthy["next_action"]["code"] == "start_chat"


def test_readiness_explains_external_server_recovery():
    payload = build_ui_status(
        setup_state={
            "hardware": _hardware(),
            "platform_support": {"status": "supported"},
            "discovery": {
                "runtime_installed": False,
                "servers": [{"kind": "llama_cpp"}],
                "gguf_models": ["model.gguf"],
                "config_exists": True,
                "enabled_slots": 1,
            },
        },
        slots_health=[{"id": "chat", "enabled": True, "health": "unhealthy", "backend_type": "remote"}],
        compute={},
        base_url="http://127.0.0.1:9000",
    )

    issue = next(issue for issue in payload["blocking_issues"] if issue["code"] == "server_stopped")
    assert "outside Imperium" in issue["message"]["en"]
    assert issue["action"]["label"]["en"] == "View guidance"
    assert payload["next_action"]["label"]["en"] == "Open server guidance"


def test_manifest_json_is_versioned(tmp_path):
    engine = _engine(tmp_path)
    engine._atomic_json(engine.manifest_path, {"schema_version": 2, "runtime": {}})
    assert json.loads(engine.manifest_path.read_text(encoding="utf-8"))["schema_version"] == 2


def test_atomic_json_tolerates_concurrent_status_requests(tmp_path):
    engine = _engine(tmp_path)
    target = engine.state_dir / "concurrent.json"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda value: engine._atomic_json(target, {"value": value}), range(20)))
    assert json.loads(target.read_text(encoding="utf-8"))["value"] in range(20)
    assert not list(target.parent.glob(".concurrent.json.*.tmp"))


def test_apply_restores_config_when_final_check_fails(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    engine.config_path.write_text("original: true\n", encoding="utf-8")
    monkeypatch.setattr(
        engine,
        "plan",
        lambda _payload: {
            "backend": "existing",
            "runtime_channel": "recommended",
            "model": None,
            "steps": [{"action": "write_config"}, {"action": "smoke_test"}],
        },
    )
    monkeypatch.setattr(
        engine,
        "write_config",
        lambda *_args, **_kwargs: engine.config_path.write_text("changed: true\n", encoding="utf-8") or {},
    )
    monkeypatch.setattr(engine, "smoke", lambda: {"ok": False, "checks": {"route": False}})

    with pytest.raises(SetupError, match="Final checks failed"):
        engine.apply({"confirm_download": True, "confirm_write": True})

    assert engine.config_path.read_text(encoding="utf-8") == "original: true\n"


def test_failed_vulkan_validation_requires_explicit_cpu_retry(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    monkeypatch.setattr(
        engine,
        "plan",
        lambda _payload: {
            "backend": "vulkan",
            "runtime_channel": "recommended",
            "model": None,
            "steps": [{"action": "start_runtime"}],
        },
    )
    monkeypatch.setattr(
        engine,
        "start_managed",
        lambda **_kwargs: (_ for _ in ()).throw(SetupError("runtime_start_failed", "device init failed")),
    )

    with pytest.raises(SetupError) as caught:
        engine.apply({"confirm_download": True, "confirm_write": True})

    assert caught.value.code == "vulkan_validation_failed"
    assert caught.value.payload()["remediation"][0] == "retry_cpu"


def test_offline_mode_refuses_an_unpacked_download(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    monkeypatch.setenv("IMPERIUM_OFFLINE", "1")
    asset = {"name": "missing.zip", "url": "https://invalid.example/missing.zip", "sha256": "a" * 64}

    with pytest.raises(SetupError) as caught:
        engine._fetch_asset(asset, tmp_path / "missing.zip", stage="runtime")

    assert caught.value.code == "offline_asset_missing"


def test_stop_managed_refuses_a_reused_pid(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    engine._atomic_json(
        engine.process_path,
        {
            "schema_version": 2,
            "pid": 4242,
            "process_executable": str(tmp_path / "llama-server.exe"),
            "process_created_at": 100.0,
            "model": str(tmp_path / "model.gguf"),
        },
    )

    class ReusedProcess:
        def exe(self):
            return str(tmp_path / "unrelated.exe")

        def create_time(self):
            return 200.0

        def cmdline(self):
            return [str(tmp_path / "unrelated.exe")]

    monkeypatch.setattr(engine_module.psutil, "Process", lambda _pid: ReusedProcess())

    with pytest.raises(SetupError) as caught:
        engine.stop_managed()

    assert caught.value.code == "runtime_ownership_unverified"
    assert engine.process_path.is_file()


def test_start_managed_rejects_an_unowned_server_with_the_same_model(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    runtime = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    runtime.touch()
    model.touch()
    engine.config_path.write_text(
        "global: {}\nactive_slots:\n  - id: local_default\n    port: 18080\n"
        f"    model_id: qwen3-1.7b-q8\n    model_path: '{model.as_posix()}'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(engine, "_available_runtime", lambda: {"binary": str(runtime)})
    monkeypatch.setattr(engine, "_port_open", lambda *_args: True)
    monkeypatch.setattr(engine, "_server_models", lambda *_args: ["qwen3-1.7b-q8"])

    with pytest.raises(SetupError) as caught:
        engine.start_managed()

    assert caught.value.code == "port_in_use"


def test_start_managed_stops_a_process_that_misses_health(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    runtime = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    runtime.touch()
    model.touch()
    engine.config_path.write_text(
        "global:\n  startup_timeout: 600\nactive_slots:\n  - id: local_default\n    port: 18080\n"
        f"    model_id: qwen3-1.7b-q8\n    model_path: '{model.as_posix()}'\n",
        encoding="utf-8",
    )
    stopped = []

    class FailedBackend:
        def __init__(self, config):
            assert config["startup_timeout"] == 600

        async def start_slot(self, _slot_id, _config):
            return type("Status", (), {"running": False, "healthy": False, "error": "not ready"})()

        async def stop_slot(self, slot_id):
            stopped.append(slot_id)
            return True

    monkeypatch.setattr(engine, "_available_runtime", lambda: {"binary": str(runtime)})
    monkeypatch.setattr(engine, "_port_open", lambda *_args: False)
    monkeypatch.setattr(subprocess_backend_module, "SubprocessBackend", FailedBackend)

    with pytest.raises(SetupError) as caught:
        engine.start_managed()

    assert caught.value.code == "runtime_start_failed"
    assert stopped == ["local_default"]
