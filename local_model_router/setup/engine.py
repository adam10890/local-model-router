"""Safe first-run planning and managed downloads for Windows-first setup."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from importlib.resources import files
from pathlib import Path
import sys
from typing import Any

import psutil
import yaml

from .hardware import collect_hardware_profile
from local_model_router.cookbook.engine import assess_catalog_model


# ponytail: setup state is low-throughput; use per-path locks if writes ever contend.
_JSON_WRITE_LOCK = threading.Lock()


class SetupError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def payload(self) -> dict[str, Any]:
        actions = {
            "release_lookup_failed": ["retry", "use_recommended", "use_offline_pack", "use_existing_server"],
            "download_failed": ["retry", "use_offline_pack", "use_existing_server"],
            "checksum_mismatch": ["replace_offline_asset", "retry"],
            "port_in_use": ["stop_conflicting_service", "use_existing_server"],
            "platform_not_supported": ["use_existing_server"],
            "backend_unavailable": ["choose_supported_backend", "use_existing_server"],
            "vulkan_validation_failed": ["retry_cpu", "open_diagnostics", "use_existing_server"],
            "offline_asset_missing": ["add_offline_pack", "disable_offline_mode", "use_existing_server"],
            "runtime_ownership_unverified": ["open_diagnostics", "stop_runtime_manually"],
            "model_incompatible": ["choose_another_model", "use_existing_server"],
            "insufficient_available_memory": ["close_other_apps", "rescan", "retry", "use_existing_server"],
        }.get(self.code, ["retry", "open_diagnostics"])
        return {"error": self.code, "detail": self.message, "remediation": actions}


def _json_resource(name: str) -> dict[str, Any]:
    return json.loads(files("local_model_router.setup").joinpath(name).read_text(encoding="utf-8"))


def default_home() -> Path:
    override = os.environ.get("IMPERIUM_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve(strict=False)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if os.name == "nt" and local:
        return Path(local) / "Imperium"
    return Path.home() / ".imperium"


class SetupEngine:
    """Stateful setup coordinator; callers explicitly confirm every download."""

    def __init__(self, *, home: str | Path | None = None, config_path: str | Path | None = None) -> None:
        self.home = Path(home or default_home()).expanduser().resolve(strict=False)
        self.runtime_dir = self.home / "runtime" / "llama.cpp"
        self.models_dir = Path(
            os.environ.get("LLAMA_MODELS_DIR", "").strip() or self.home / "models"
        ).expanduser().resolve(strict=False)
        self.state_dir = self.home / "state"
        self.config_path = Path(config_path).resolve(strict=False) if config_path else self.home / "conf" / "llama_cpp_servers.yaml"
        self.manifest_path = self.state_dir / "installation-manifest.json"
        self.inventory_path = self.state_dir / "model-inventory.json"
        self.hardware_path = self.state_dir / "hardware-profile.json"
        self.process_path = self.state_dir / "runtime-process.json"
        configured_offline = os.environ.get("IMPERIUM_OFFLINE_DIR", "").strip()
        packaged_offline = Path(sys.executable).resolve(strict=False).parents[2] / "offline"
        self.offline_dirs = [
            path
            for path in (
                Path(configured_offline).expanduser().resolve(strict=False) if configured_offline else None,
                self.home / "offline",
                packaged_offline,
            )
            if path is not None
        ]
        self._events: list[dict[str, Any]] = []
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._apply_lock = threading.Lock()
        self._runtime_backend: Any = None

    @property
    def catalog(self) -> dict[str, Any]:
        return _json_resource("model_catalog.json")

    @property
    def runtime_catalog(self) -> dict[str, Any]:
        return _json_resource("runtime_catalog.json")

    def _emit(self, stage: str, status: str, detail: str = "", progress: float | None = None) -> None:
        with self._lock:
            self._events.append(
                {
                    "id": len(self._events) + 1,
                    "at": time.time(),
                    "stage": stage,
                    "status": status,
                    "detail": detail,
                    "progress": progress,
                }
            )
            self._events = self._events[-200:]

    def events(self, after: int = 0) -> dict[str, Any]:
        with self._lock:
            rows = [event for event in self._events if int(event["id"]) > after]
        return {"events": rows, "cancelled": self._cancel.is_set()}

    def cancel(self) -> None:
        self._cancel.set()
        self._emit("setup", "cancelled", "Cancellation requested")

    def reset_cancel(self) -> None:
        self._cancel.clear()

    def _managed_runtime(self) -> dict[str, Any] | None:
        manifest = self._read_json(self.manifest_path)
        runtime = manifest.get("runtime") if isinstance(manifest, dict) else None
        if not isinstance(runtime, dict):
            return None
        binary = Path(str(runtime.get("binary") or ""))
        return runtime if binary.is_file() else None

    def _available_runtime(self) -> dict[str, Any] | None:
        managed = self._managed_runtime()
        if managed:
            return managed
        binary = shutil.which("llama-server") or shutil.which("llama-server.exe")
        if not binary:
            return None
        return {
            "tag": "system",
            "backend": "existing_binary",
            "binary": str(Path(binary).resolve(strict=False)),
            "source": "PATH",
            "channel": "installed",
        }

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        with _JSON_WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(
                f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def hardware(self, *, refresh: bool = False) -> dict[str, Any]:
        if not refresh:
            cached = self._read_json(self.hardware_path)
            if cached.get("schema_version") == 2 and cached.get("scan_complete") is True:
                return cached
        profile = collect_hardware_profile(target_dir=self.models_dir)
        self._atomic_json(self.hardware_path, profile)
        return profile

    def discover(self) -> dict[str, Any]:
        managed = self._managed_runtime()
        path_binary = shutil.which("llama-server") or shutil.which("llama-server.exe")
        ggufs = []
        if self.models_dir.is_dir():
            ggufs = [str(path) for path in sorted(self.models_dir.rglob("*.gguf"))[:200]]
        config_exists = self.config_path.is_file()
        slots = []
        if config_exists:
            try:
                data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
                slots = [slot for slot in data.get("active_slots", []) if isinstance(slot, dict)]
            except (OSError, yaml.YAMLError, AttributeError):
                slots = []
        ports = {port: self._port_open("127.0.0.1", port) for port in (8080, 11434, 1234, 12434)}
        servers = []
        if ports[8080]:
            models = self._server_models("127.0.0.1", 8080)
            if models is not None:
                servers.append({"kind": "llama.cpp_or_compatible", "url": "http://127.0.0.1:8080/v1", "source": "models_api", "confidence": "high", "models": models})
        if ports[11434]:
            models = self._server_models("127.0.0.1", 11434)
            if models is not None:
                servers.append({"kind": "ollama", "url": "http://127.0.0.1:11434/v1", "source": "models_api", "confidence": "high", "models": models})
        if ports[1234]:
            models = self._server_models("127.0.0.1", 1234)
            if models is not None:
                servers.append({"kind": "lm_studio", "url": "http://127.0.0.1:1234/v1", "source": "models_api", "confidence": "high", "models": models})
        if ports[12434]:
            models = self._server_models("127.0.0.1", 12434)
            if models is not None:
                servers.append({"kind": "docker_model_runner", "url": "http://127.0.0.1:12434/engines/v1", "source": "models_api", "confidence": "high", "models": models})
        offline_assets = sorted(
            {path.name for directory in self.offline_dirs if directory.is_dir() for path in directory.iterdir() if path.is_file()}
        )
        return {
            "managed_runtime": managed,
            "path_runtime": path_binary,
            "runtime_installed": bool(managed or path_binary),
            "existing_server_available": bool(servers),
            "runtime_available": bool(managed or path_binary or servers),
            "models_dir": str(self.models_dir),
            "gguf_models": ggufs,
            "config_path": str(self.config_path),
            "config_exists": config_exists,
            "enabled_slots": sum(1 for slot in slots if slot.get("enabled", True)),
            "docker_available": bool(shutil.which("docker")),
            "ollama_available": bool(shutil.which("ollama")),
            "servers": servers,
            "ports": {str(port): occupied for port, occupied in ports.items()},
            "offline": {
                "available": bool(offline_assets),
                "directories": [str(path) for path in self.offline_dirs if path.is_dir()],
                "assets": offline_assets,
            },
        }

    @staticmethod
    def _port_open(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            return False

    @staticmethod
    def _server_models(host: str, port: int) -> list[str] | None:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/v1/models", timeout=3) as response:  # noqa: S310
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            return None
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return None
        return [str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")]

    def _platform_entry(self, hardware: dict[str, Any]) -> dict[str, Any] | None:
        platform_data = hardware.get("platform") or {}
        return next(
            (
                row
                for row in self.runtime_catalog.get("platforms", [])
                if row.get("os") == platform_data.get("os") and row.get("arch") == platform_data.get("arch")
            ),
            None,
        )

    def backend_candidates(self, hardware: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        hardware = hardware or self.hardware()
        platform_entry = self._platform_entry(hardware)
        accelerators = hardware.get("accelerators") or {}
        evidence = hardware.get("accelerator_evidence") or {}
        rows = []
        for backend, details in ((platform_entry or {}).get("backends") or {}).items():
            status = str(details.get("status") or platform_entry.get("status") or "planned")
            capability = "cuda" if backend.startswith("cuda") else "hip" if backend == "rocm" else backend
            detected = backend == "cpu" or bool(accelerators.get(capability))
            driver_ok = True
            if details.get("min_driver"):
                driver = next((str(gpu.get("driver_version") or "") for gpu in hardware.get("gpus", []) if gpu.get("driver_version")), "")
                try:
                    driver_ok = tuple(int(part) for part in driver.split(".")[:2]) >= tuple(
                        int(part) for part in str(details["min_driver"]).split(".")[:2]
                    )
                except ValueError:
                    driver_ok = False
            eligible = status == "supported" and detected and driver_ok
            assets = details.get("assets", [])
            offline_ready = bool(assets) and all(
                self._offline_asset(str(asset.get("name") or "")) for asset in assets
            )
            reason = (
                "eligible"
                if eligible
                else "driver_incompatible_or_unknown"
                if status == "supported" and detected and not driver_ok
                else str(details.get("reason_code") or "platform_planned")
                if status != "supported"
                else "hardware_capability_missing"
            )
            rows.append(
                {
                    "id": backend,
                    "status": status,
                    "eligible": eligible,
                    "reason_code": reason,
                    "confidence": (evidence.get(capability) or {}).get("confidence", "high" if backend == "cpu" else "low"),
                    "assets": assets,
                    "offline_ready": offline_ready,
                    "min_driver": details.get("min_driver"),
                }
            )
        priority = {"cuda12": 0, "vulkan": 1, "cpu": 2}
        offline_pack_present = any(row["offline_ready"] for row in rows)
        rows.sort(
            key=lambda row: (
                0 if row["eligible"] else 1,
                0 if not offline_pack_present or row["offline_ready"] else 1,
                priority.get(row["id"], 9),
                row["id"],
            )
        )
        if rows:
            first = next((row for row in rows if row["eligible"]), None)
            if first:
                first["recommended"] = True
        return rows

    def models(
        self,
        hardware: dict[str, Any] | None = None,
        backend: str | None = None,
    ) -> list[dict[str, Any]]:
        hardware = hardware or self.hardware()
        backend = backend or next(
            (row["id"] for row in self.backend_candidates(hardware) if row.get("eligible")),
            "cpu",
        )
        installed = {Path(path).name for path in self.discover()["gguf_models"]}
        rows = []
        for raw in self.catalog.get("models", []):
            row = dict(raw)
            row["installed"] = row.get("filename") in installed
            row["backend"] = backend
            row.update(assess_catalog_model(row, hardware, backend))
            row["fit_reason_i18n"] = {
                "en": row["fit_reason"],
                "he": "המודל מתאים לחומרה ול־backend שנבחרו"
                if row["fit"] != "incompatible"
                else "הזיכרון שזוהה נמוך מדרישת המינימום הבטוחה",
            }
            rows.append(row)
        return rows

    def recommendation(
        self,
        hardware: dict[str, Any] | None = None,
        backend: str | None = None,
    ) -> dict[str, Any] | None:
        candidates = [row for row in self.models(hardware, backend) if row["fit"] != "incompatible"]
        if not candidates:
            return None
        rank = {"full_gpu": 3, "partial_offload": 2, "cpu": 1}
        candidates.sort(
            key=lambda row: (
                bool(row.get("first_run_default")),
                rank.get(row["fit"], 0),
                -float(row.get("size_gb") or 0),
            ),
            reverse=True,
        )
        return candidates[0]

    def state(self, *, refresh_hardware: bool = False) -> dict[str, Any]:
        hardware = self.hardware(refresh=refresh_hardware)
        discovery = self.discover()
        backends = self.backend_candidates(hardware)
        recommended_backend = next((row["id"] for row in backends if row.get("eligible")), None)
        models = self.models(hardware, recommended_backend) if recommended_backend else []
        recommendation = self.recommendation(hardware, recommended_backend) if recommended_backend else None
        platform_entry = self._platform_entry(hardware)
        return {
            "schema_version": 2,
            "home": str(self.home),
            "hardware": hardware,
            "discovery": discovery,
            "platform_support": {
                "status": str((platform_entry or {}).get("status") or "unsupported"),
                "reason_code": "platform_supported" if (platform_entry or {}).get("status") == "supported" else "platform_planned",
            },
            "backend_candidates": backends,
            "recommended_backend": recommended_backend,
            "recommendation": recommendation,
            "models": models,
            "experimental_candidates": self.catalog.get("experimental_candidates", []),
            "setup_complete": bool(discovery["runtime_available"] and discovery["enabled_slots"]),
        }

    @staticmethod
    def _require_available_memory(model: dict[str, Any] | None, hardware: dict[str, Any]) -> float | None:
        if not model:
            return None
        available_mb = (hardware.get("ram") or {}).get("available_mb")
        if not isinstance(available_mb, (int, float)) or isinstance(available_mb, bool):
            return None
        required_gb = (
            float(model.get("size_gb") or 0)
            + float(model.get("estimated_kv_cache_gb") or 0)
            + 1.0
        )
        available_gb = float(available_mb) / 1024
        if available_gb < required_gb:
            raise SetupError(
                "insufficient_available_memory",
                f"Setup needs about {required_gb:.1f} GB of available RAM but only {available_gb:.1f} GB is available. Close other model servers or applications, then scan again.",
            )
        return required_gb

    def plan(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = request or {}
        state = self.state(refresh_hardware=True)
        requested_backend = str(request.get("backend") or state.get("recommended_backend") or "")
        if not requested_backend:
            raise SetupError("platform_not_supported", "Managed installation is planned for this platform; use an existing local server")
        if requested_backend != "existing":
            candidate = next((row for row in state["backend_candidates"] if row["id"] == requested_backend), None)
            if not candidate or not candidate.get("eligible"):
                reason = (candidate or {}).get("reason_code") or "backend_not_in_platform_matrix"
                raise SetupError("backend_unavailable", f"Backend {requested_backend} is not available: {reason}")
        models = self.models(state["hardware"], requested_backend) if requested_backend != "existing" else []
        recommendation = self.recommendation(state["hardware"], requested_backend) if models else None
        model_id = "" if requested_backend == "existing" else str(
            request.get("model_id") or (recommendation or {}).get("id") or ""
        )
        model = next((row for row in models if row["id"] == model_id), None)
        if model_id and not model:
            raise SetupError("unknown_model", f"Unknown catalog model: {model_id}")
        if model and model.get("fit") == "incompatible":
            raise SetupError("model_incompatible", str(model.get("fit_reason") or "The selected model does not fit"))
        backend = requested_backend
        channel = str(request.get("runtime_channel") or "recommended")
        if channel not in {"recommended", "latest"}:
            raise SetupError("unknown_channel", f"Unknown llama.cpp channel: {channel}")
        try:
            managed_port = int(request.get("port") or 8080)
        except (TypeError, ValueError) as exc:
            raise SetupError("invalid_port", "The managed llama.cpp port must be a number") from exc
        if backend != "existing" and not 1024 <= managed_port <= 65535:
            raise SetupError("invalid_port", "The managed llama.cpp port must be between 1024 and 65535")
        model_already_served = any(
            model_id in (server.get("models") or [])
            and urllib.parse.urlparse(str(server.get("url") or "")).port == managed_port
            for server in state["discovery"].get("servers", [])
        )
        memory_required_gb = None if model_already_served else self._require_available_memory(model, state["hardware"])
        steps = []
        if backend != "existing" and not state["discovery"]["runtime_installed"]:
            runtime_candidate = next(
                row for row in state["backend_candidates"] if row["id"] == backend
            )
            steps.append(
                {
                    "action": "install_runtime",
                    "backend": backend,
                    "channel": channel,
                    "source": "offline"
                    if channel == "recommended" and runtime_candidate.get("offline_ready")
                    else "online",
                    "requires_confirmation": True,
                }
            )
        if model and not model["installed"]:
            steps.append(
                {
                    "action": "download_model",
                    "model_id": model["id"],
                    "size_gb": model["size_gb"],
                    "license": model["license"],
                    "source_url": model["source_url"],
                    "sha256": model["sha256"],
                    "source": "offline" if self._offline_asset(str(model["filename"])) else "online",
                    "requires_confirmation": True,
                }
            )
        required_gb = float((model or {}).get("size_gb") or 0) + 1.0
        free_gb = (state.get("hardware", {}).get("disk") or {}).get("free_gb")
        if isinstance(free_gb, (int, float)) and free_gb < required_gb:
            raise SetupError(
                "insufficient_disk_space",
                f"Setup needs about {required_gb:.1f} GB but only {free_gb:.1f} GB is free",
            )
        steps.append({"action": "write_config", "model_id": model_id or None, "requires_confirmation": True})
        if backend != "existing":
            steps.append({"action": "start_runtime", "requires_confirmation": False})
        steps.append({"action": "smoke_test", "requires_confirmation": False})
        return {
            "schema_version": 2,
            "backend": backend,
            "runtime_channel": channel,
            "model": model,
            "steps": steps,
            "destination": {
                "runtime": str(self.runtime_dir),
                "models": str(self.models_dir),
                "config": str(self.config_path),
            },
            "existing_url": request.get("existing_url") if backend == "existing" else None,
            "port": managed_port if backend != "existing" else None,
            "platform": state["hardware"].get("platform"),
            "memory_required_gb": memory_required_gb,
        }

    @staticmethod
    def _github_release(api_url: str) -> dict[str, Any]:
        request = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github+json", "User-Agent": "Imperium-Setup"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise SetupError("release_lookup_failed", f"Could not read the official llama.cpp release: {exc}") from exc
        if not isinstance(payload, dict):
            raise SetupError("invalid_release", "The official release response was not valid")
        return payload

    @staticmethod
    def _safe_extract(bundle: zipfile.ZipFile, destination: Path) -> None:
        root = destination.resolve(strict=False)
        for member in bundle.infolist():
            target = (destination / member.filename).resolve(strict=False)
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise SetupError("runtime_archive_unsafe", "The runtime archive contains an unsafe path") from exc
        bundle.extractall(destination)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().lower()

    def _offline_asset(self, name: str) -> Path | None:
        return next((directory / name for directory in self.offline_dirs if (directory / name).is_file()), None)

    def _fetch_asset(self, asset: dict[str, Any], destination: Path, *, stage: str) -> None:
        expected = str(asset.get("sha256") or "").lower()
        if len(expected) != 64:
            raise SetupError("checksum_missing", f"No SHA-256 is pinned for {asset.get('name')}")
        if destination.is_file() and self._sha256(destination) == expected:
            self._emit(stage, "complete", f"Using verified cached {destination.name}", 1.0)
            return
        offline = self._offline_asset(str(asset["name"]))
        if offline:
            destination.parent.mkdir(parents=True, exist_ok=True)
            partial = destination.with_suffix(destination.suffix + ".part")
            shutil.copy2(offline, partial)
            if self._sha256(partial) != expected:
                partial.unlink(missing_ok=True)
                raise SetupError("checksum_mismatch", f"Checksum verification failed for offline {offline.name}")
            os.replace(partial, destination)
            self._emit(stage, "complete", f"Imported and verified {offline.name}", 1.0)
            return
        if os.environ.get("IMPERIUM_OFFLINE") == "1":
            raise SetupError(
                "offline_asset_missing",
                f"Offline mode is enabled but {asset['name']} is not available in the offline pack",
            )
        self._download(str(asset["url"]), destination, expected_sha256=expected, stage=stage)

    def _download(self, url: str, destination: Path, *, expected_sha256: str, stage: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        existing = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "Imperium-Setup"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        request = urllib.request.Request(url, headers=headers)
        self._emit(stage, "running", f"Downloading {destination.name}", 0.0)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                total = response.headers.get("Content-Length")
                total_size = existing + int(total) if total and response.status == 206 else int(total or 0)
                mode = "ab" if response.status == 206 and existing else "wb"
                downloaded = existing if mode == "ab" else 0
                with partial.open(mode) as output:
                    while True:
                        if self._cancel.is_set():
                            raise SetupError("cancelled", "Setup was cancelled")
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        downloaded += len(chunk)
                        if total_size:
                            self._emit(stage, "running", f"Downloading {destination.name}", min(0.99, downloaded / total_size))
        except SetupError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SetupError("download_failed", f"Download failed: {exc}") from exc

        if self._sha256(partial) != expected_sha256.lower():
            partial.unlink(missing_ok=True)
            raise SetupError("checksum_mismatch", f"Checksum verification failed for {destination.name}")
        os.replace(partial, destination)
        self._emit(stage, "complete", f"Downloaded and verified {destination.name}", 1.0)

    def install_runtime(self, backend: str, *, channel: str = "recommended") -> dict[str, Any]:
        if channel not in {"recommended", "latest"}:
            raise SetupError("unknown_channel", f"Unknown llama.cpp channel: {channel}")
        hardware = self.hardware()
        platform_entry = self._platform_entry(hardware)
        backend_entry = ((platform_entry or {}).get("backends") or {}).get(backend) or {}
        if platform_entry is None or platform_entry.get("status") != "supported" or backend_entry.get("status") != "supported":
            raise SetupError("backend_unavailable", f"Managed {backend} installation is not supported on this platform")
        recommended = self.runtime_catalog["recommended"]
        tag = str(recommended["tag"])
        source = str(recommended["source"])
        assets = [dict(asset) for asset in backend_entry.get("assets", [])]
        if channel == "latest":
            release = self._github_release(self.runtime_catalog["latest_api"])
            tag = str(release.get("tag_name") or "")
            source = str(release.get("html_url") or "")
            released = {str(row.get("name")): row for row in release.get("assets", []) if isinstance(row, dict)}
            latest_assets = []
            for pinned in assets:
                name = str(pinned["name"]).replace(str(recommended["tag"]), tag)
                row = released.get(name)
                digest = str((row or {}).get("digest") or "")
                if not row or not digest.startswith("sha256:"):
                    raise SetupError("runtime_asset_missing", f"The latest release does not provide a verified {name}")
                latest_assets.append(
                    {
                        "name": name,
                        "url": row["browser_download_url"],
                        "sha256": digest.split(":", 1)[1],
                        "size_bytes": row.get("size"),
                    }
                )
            assets = latest_assets
        if not assets:
            raise SetupError("runtime_asset_missing", f"No runtime assets are defined for {backend}")
        archives = []
        for asset in assets:
            archive = self.runtime_dir / "downloads" / str(asset["name"])
            self._fetch_asset(asset, archive, stage="runtime")
            archives.append(archive)

        target = self.runtime_dir / "versions" / f"{tag}-{backend}"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="imperium-llama-", dir=str(self.runtime_dir)))
        try:
            for archive in archives:
                with zipfile.ZipFile(archive) as bundle:
                    self._safe_extract(bundle, staging)
            candidates = list(staging.rglob("llama-server.exe"))
            if not candidates:
                raise SetupError("runtime_invalid", "The runtime archive did not contain llama-server.exe")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not (target / "llama-server.exe").is_file():
                    raise SetupError("runtime_target_invalid", f"The existing runtime target is incomplete: {target}")
            else:
                binary_parent = candidates[0].parent
                for dll in staging.rglob("*.dll"):
                    destination = binary_parent / dll.name
                    if not destination.exists():
                        shutil.copy2(dll, destination)
                shutil.move(str(binary_parent), target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        binary = target / "llama-server.exe"
        manifest = self._read_json(self.manifest_path)
        previous = manifest.get("runtime")
        manifest.update(
            {
                "schema_version": 2,
                "updated_at": time.time(),
                "previous_runtime": previous,
                "runtime": {
                    "tag": tag,
                    "backend": backend,
                    "binary": str(binary),
                    "source": source,
                    "channel": channel,
                    "assets": [
                        {"name": asset["name"], "sha256": asset["sha256"], "size_bytes": asset.get("size_bytes")}
                        for asset in assets
                    ],
                },
            }
        )
        self._atomic_json(self.manifest_path, manifest)
        return manifest["runtime"]

    def update_status(self) -> dict[str, Any]:
        release = self._github_release(self.runtime_catalog["latest_api"])
        latest = str(release.get("tag_name") or "")
        installed = str((self._managed_runtime() or {}).get("tag") or "")
        return {
            "installed": installed or None,
            "latest": latest or None,
            "update_available": bool(latest and installed and latest != installed),
            "runtime_installed": bool(installed),
            "source": release.get("html_url"),
        }

    def download_model(self, model_id: str) -> dict[str, Any]:
        model = next((row for row in self.catalog.get("models", []) if row.get("id") == model_id), None)
        if not model:
            raise SetupError("unknown_model", f"Unknown catalog model: {model_id}")
        checksum = str(model.get("sha256") or "")
        if len(checksum) != 64:
            raise SetupError("model_checksum_missing", "The approved catalog does not contain a model SHA-256")
        destination = self.models_dir / str(model["filename"])
        self._fetch_asset(
            {"name": model["filename"], "url": model["download_url"], "sha256": checksum},
            destination,
            stage="model",
        )
        inventory = self._read_json(self.inventory_path)
        items = [item for item in inventory.get("models", []) if item.get("id") != model_id]
        items.append({"id": model_id, "path": str(destination), "sha256": checksum, "installed_at": time.time()})
        inventory = {"schema_version": 2, "models": items}
        self._atomic_json(self.inventory_path, inventory)
        return items[-1]

    def write_config(
        self,
        model_id: str | None,
        backend: str,
        *,
        existing_url: str | None = None,
        managed_port: int = 8080,
    ) -> dict[str, Any]:
        runtime = self._available_runtime()
        model = next((row for row in self.catalog.get("models", []) if row.get("id") == model_id), None)
        model_path = self.models_dir / str(model["filename"]) if model else None
        if backend != "existing" and not runtime:
            raise SetupError("runtime_missing", "Install or select llama.cpp before writing the managed configuration")
        if model and not model_path.is_file():
            raise SetupError("model_missing", "Download the selected model before writing the configuration")
        host = "127.0.0.1"
        port = managed_port
        if backend == "existing":
            parsed = urllib.parse.urlparse(existing_url or "http://127.0.0.1:8080/v1")
            host = parsed.hostname or "127.0.0.1"
            if host not in {"127.0.0.1", "localhost", "::1"}:
                raise SetupError("existing_server_not_local", "First-run existing servers must use a loopback address")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        payload = {
            "global": {
                "backend": "remote" if backend == "existing" else "subprocess",
                "llama_cpp_path": str(Path(str(runtime["binary"])).parent) if runtime else "",
                "server_binary": "llama-server",
                "models_dir": str(self.models_dir),
                "log_dir": str(self.state_dir / "logs"),
                "startup_timeout": 600,
            },
            "active_slots": [
                {
                    "id": "local_default",
                    "host": host,
                    "port": port,
                    "role": "chat",
                    "enabled": True,
                    "model_id": model_id or "local",
                    "model_path": str(model_path) if model_path else "",
                    "context_size": int((model or {}).get("runtime_context") or 4096),
                    "gpu_layers": -1 if backend != "cpu" else 0,
                    "parallel_slots": 1,
                    "jinja": bool(model and "tools" in model.get("capabilities", [])),
                    "supports_tools": bool(model and "tools" in model.get("capabilities", [])),
                    "supports_json_mode": bool(model and "json" in model.get("capabilities", [])),
                }
            ],
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if self.config_path.exists():
            backup = self.config_path.with_suffix(self.config_path.suffix + f".bak-{int(time.time())}")
            shutil.copy2(self.config_path, backup)
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        os.replace(temporary, self.config_path)
        self._emit("config", "complete", "Managed configuration saved", 1.0)
        return payload

    def start_managed(self, *, visible_terminal: bool = False) -> dict[str, Any]:
        runtime = self._available_runtime()
        if not runtime:
            raise SetupError("runtime_missing", "No managed llama.cpp runtime is installed")
        try:
            payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            slot = next(slot for slot in payload.get("active_slots", []) if slot.get("enabled", True))
        except (OSError, yaml.YAMLError, StopIteration, TypeError, AttributeError) as exc:
            raise SetupError("configuration_invalid", "The managed server configuration is unavailable") from exc
        model_path = Path(str(slot.get("model_path") or ""))
        if not model_path.is_file():
            raise SetupError("model_missing", "The configured GGUF model is unavailable")
        port = int(slot.get("port") or 8080)
        if self._port_open("127.0.0.1", port):
            process_state = self._read_json(self.process_path)
            owned = False
            try:
                process = psutil.Process(int(process_state.get("pid") or 0))
                expected_executable = str(process_state.get("process_executable") or "").strip()
                expected_created_at = process_state.get("process_created_at")
                expected_model = str(process_state.get("model") or "").strip()
                actual_executable = str(Path(process.exe()).resolve(strict=False)).casefold()
                recorded_executable = str(Path(expected_executable).resolve(strict=False)).casefold()
                recorded_model = str(Path(expected_model).resolve(strict=False)).casefold()
                command = {str(Path(arg).resolve(strict=False)).casefold() for arg in process.cmdline() if arg}
                owned = (
                    bool(expected_executable)
                    and isinstance(expected_created_at, (int, float))
                    and int(process_state.get("port") or 0) == port
                    and actual_executable == recorded_executable
                    and abs(process.create_time() - float(expected_created_at)) < 2.0
                    and recorded_model in command
                )
            except (TypeError, ValueError, psutil.Error):
                owned = False
            models = self._server_models("127.0.0.1", port)
            if owned and models is not None and str(slot.get("model_id") or "local") in models:
                return {"ok": True, "already_running": True, "port": port, "models": models}
            raise SetupError("port_in_use", f"Port {port} is occupied by a different or unverifiable service")
        from local_model_router.helpers.backends.subprocess_backend import SubprocessBackend

        global_config = dict(payload.get("global") or {})
        slot_config = {**slot, "visible_terminal": visible_terminal}
        self._runtime_backend = SubprocessBackend(global_config)
        slot_id = str(slot.get("id") or "local_default")
        status = asyncio.run(self._runtime_backend.start_slot(slot_id, slot_config))
        if not status.running or not status.healthy or status.error:
            asyncio.run(self._runtime_backend.stop_slot(slot_id))
            raise SetupError("runtime_start_failed", status.error or "llama-server did not become healthy")
        try:
            process = psutil.Process(int(status.pid))
            process_created_at = process.create_time()
            process_executable = process.exe()
        except (TypeError, ValueError, psutil.Error) as exc:
            asyncio.run(self._runtime_backend.stop_slot(slot_id))
            raise SetupError("runtime_start_failed", "Could not record managed process ownership") from exc
        binary = Path(str(runtime["binary"]))
        log_path = Path(str(global_config.get("log_dir") or self.state_dir / "logs")) / f"{slot.get('id') or 'local_default'}.log"
        process_state = {
            "schema_version": 2,
            "pid": status.pid,
            "binary": str(binary),
            "process_executable": process_executable,
            "process_created_at": process_created_at,
            "model": str(model_path),
            "port": port,
            "visible_terminal": visible_terminal,
            "started_at": time.time(),
            "log": str(log_path),
        }
        self._atomic_json(self.process_path, process_state)
        self._emit("runtime_start", "complete", f"llama-server is listening on port {port}", 1.0)
        return {"ok": True, **process_state}

    def stop_managed(self) -> dict[str, Any]:
        process_state = self._read_json(self.process_path)
        pid = process_state.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return {"ok": True, "already_stopped": True}
        if self._runtime_backend is not None:
            stopped = asyncio.run(self._runtime_backend.stop_slot("local_default"))
            if stopped:
                self.process_path.unlink(missing_ok=True)
                self._emit("runtime_stop", "complete", "Managed llama-server stopped", 1.0)
                return {"ok": True, "pid": pid}
        try:
            process = psutil.Process(pid)
            expected_executable = str(process_state.get("process_executable") or "").strip()
            expected_created_at = process_state.get("process_created_at")
            expected_model = str(process_state.get("model") or "").strip()
            if not expected_executable or not isinstance(expected_created_at, (int, float)):
                raise SetupError(
                    "runtime_ownership_unverified",
                    "The saved runtime state predates process ownership checks; refusing to stop this PID",
                )
            actual_executable = str(Path(process.exe()).resolve(strict=False)).casefold()
            recorded_executable = str(Path(expected_executable).resolve(strict=False)).casefold()
            same_start = abs(process.create_time() - float(expected_created_at)) < 2.0
            command = {str(Path(arg).resolve(strict=False)).casefold() for arg in process.cmdline() if arg}
            recorded_model = str(Path(expected_model).resolve(strict=False)).casefold() if expected_model else ""
            if actual_executable != recorded_executable or not same_start or (recorded_model and recorded_model not in command):
                raise SetupError(
                    "runtime_ownership_unverified",
                    "The saved PID no longer matches the Imperium-managed llama-server",
                )
            process.terminate()
            try:
                process.wait(timeout=10)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
        except SetupError:
            raise
        except psutil.Error as exc:
            raise SetupError("runtime_stop_failed", f"Could not stop managed llama-server: {exc}") from exc
        self.process_path.unlink(missing_ok=True)
        self._emit("runtime_stop", "complete", "Managed llama-server stopped", 1.0)
        return {"ok": True, "pid": pid}

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirm_download") is not True or payload.get("confirm_write") is not True:
            raise SetupError("confirmation_required", "Runtime/model downloads and configuration writes require explicit confirmation")
        if not self._apply_lock.acquire(blocking=False):
            raise SetupError("setup_busy", "Another setup operation is already running")
        try:
            plan = self.plan(payload)
            backend = str(plan["backend"])
            channel = str(plan.get("runtime_channel") or "recommended")
            model_id = str((plan.get("model") or {}).get("id") or "") or None
            self.reset_cancel()
            results = []
            original_config = self.config_path.read_bytes() if self.config_path.is_file() else None
            config_written = False
            runtime_started = False
            try:
                for step in plan["steps"]:
                    action = step["action"]
                    if action == "install_runtime":
                        results.append({"action": action, "result": self.install_runtime(backend, channel=channel)})
                    elif action == "download_model" and model_id:
                        results.append({"action": action, "result": self.download_model(model_id)})
                    elif action == "write_config":
                        results.append(
                            {
                                "action": action,
                                "result": self.write_config(
                                    model_id,
                                    backend,
                                    existing_url=str(payload.get("existing_url") or "") or None,
                                    managed_port=int(plan.get("port") or 8080),
                                ),
                            }
                        )
                        config_written = True
                    elif action == "start_runtime":
                        if not self._port_open("127.0.0.1", int(plan.get("port") or 8080)):
                            self._require_available_memory(plan.get("model"), self.hardware(refresh=True))
                        try:
                            start_result = self.start_managed(
                                visible_terminal=payload.get("launch_mode") == "terminal"
                            )
                        except SetupError as exc:
                            if backend == "vulkan" and exc.code == "runtime_start_failed":
                                raise SetupError(
                                    "vulkan_validation_failed",
                                    f"Vulkan could not be validated: {exc.message}",
                                ) from exc
                            raise
                        runtime_started = not bool(start_result.get("already_running"))
                        results.append({"action": action, "result": start_result})
                    elif action == "smoke_test":
                        smoke_result = self.smoke()
                        results.append({"action": action, "result": smoke_result})
                        if not smoke_result.get("ok"):
                            failed = ", ".join(
                                code for code, ok in smoke_result.get("checks", {}).items() if not ok
                            )
                            raise SetupError("smoke_failed", f"Final checks failed: {failed}")
                return {"ok": True, "results": results, "state": self.state(refresh_hardware=True)}
            except Exception:
                if runtime_started:
                    try:
                        self.stop_managed()
                    except SetupError:
                        pass
                if config_written:
                    if original_config is None:
                        self.config_path.unlink(missing_ok=True)
                    else:
                        rollback = self.config_path.with_name(
                            f".{self.config_path.name}.{os.getpid()}.{threading.get_ident()}.rollback"
                        )
                        try:
                            rollback.write_bytes(original_config)
                            os.replace(rollback, self.config_path)
                        finally:
                            rollback.unlink(missing_ok=True)
                raise
        finally:
            self._apply_lock.release()

    def smoke(self) -> dict[str, Any]:
        discovery = self.discover()
        server_health = False
        models_api = False
        model_identity = False
        chat_completion = False
        tool_calling = True
        tool_calling_required = False
        slot: dict[str, Any] = {}
        global_config: dict[str, Any] = {}
        if self.config_path.is_file():
            try:
                payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
                global_config = payload.get("global") or {}
                slot = next(row for row in payload.get("active_slots", []) if row.get("enabled", True))
            except (OSError, yaml.YAMLError, StopIteration, TypeError, AttributeError):
                slot = {}
        if slot:
            host = str(slot.get("host") or "127.0.0.1")
            port = int(slot.get("port") or 8080)
            base = f"http://{host}:{port}"
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=5) as response:  # noqa: S310
                    server_health = 200 <= response.status < 300
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                server_health = False
            try:
                with urllib.request.urlopen(f"{base}/v1/models", timeout=8) as response:  # noqa: S310
                    models_payload = json.load(response)
                    models_api = 200 <= response.status < 300 and isinstance(models_payload, dict)
                    model_ids = [
                        str(row.get("id"))
                        for row in models_payload.get("data", [])
                        if isinstance(row, dict) and row.get("id")
                    ]
                    model_identity = global_config.get("backend") == "remote" or str(slot.get("model_id") or "local") in model_ids
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                models_api = False
            if models_api and not server_health:
                server_health = True
            if server_health and models_api:
                body = json.dumps(
                    {
                        "model": str(slot.get("model_id") or "local"),
                        "messages": [{"role": "user", "content": "Reply with OK."}],
                        "max_tokens": 4,
                        "temperature": 0,
                        "stream": False,
                        "chat_template_kwargs": {"enable_thinking": False},
                    }
                ).encode("utf-8")
                request = urllib.request.Request(
                    f"{base}/v1/chat/completions",
                    data=body,
                    headers={"Content-Type": "application/json", "User-Agent": "Imperium-Smoke"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                        chat_payload = json.load(response)
                        chat_completion = 200 <= response.status < 300 and bool(chat_payload.get("choices"))
                except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                    chat_completion = False
            model = next(
                (row for row in self.catalog.get("models", []) if row.get("id") == slot.get("model_id")),
                None,
            )
            tool_calling_required = bool(model and model.get("tool_calling_smoke_required"))
            if tool_calling_required and server_health and models_api:
                tool_body = json.dumps(
                    {
                        "model": str(slot.get("model_id")),
                        "messages": [{"role": "user", "content": "Call imperium_ping now."}],
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "imperium_ping",
                                    "description": "Return a setup verification ping",
                                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                                },
                            }
                        ],
                        "tool_choice": "required",
                        "chat_template_kwargs": {"enable_thinking": False},
                        "max_tokens": 64,
                        "temperature": 0,
                        "stream": False,
                    }
                ).encode("utf-8")
                tool_request = urllib.request.Request(
                    f"{base}/v1/chat/completions",
                    data=tool_body,
                    headers={"Content-Type": "application/json", "User-Agent": "Imperium-Tool-Smoke"},
                )
                try:
                    with urllib.request.urlopen(tool_request, timeout=90) as response:  # noqa: S310
                        tool_payload = json.load(response)
                    calls = (((tool_payload.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or [])
                    tool_calling = any(
                        ((call.get("function") or {}).get("name") == "imperium_ping")
                        for call in calls
                        if isinstance(call, dict)
                    )
                except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                    tool_calling = False
        checks = {
            "runtime": discovery["runtime_available"] or (global_config.get("backend") == "remote" and server_health),
            "config": discovery["config_exists"],
            "model": bool(discovery["gguf_models"]) or models_api,
            "server": server_health,
            "models_api": models_api,
            "model_identity": model_identity,
            "chat_completion": chat_completion,
            "tool_calling": tool_calling,
        }
        if tool_calling_required and tool_calling:
            inventory = self._read_json(self.inventory_path)
            for item in inventory.get("models", []):
                if item.get("id") == slot.get("model_id"):
                    item["tool_calling_verified"] = True
                    item["verified_at"] = time.time()
            if inventory:
                self._atomic_json(self.inventory_path, inventory)
        return {"ok": all(checks.values()), "checks": checks, "tool_calling_required": tool_calling_required}

    def rollback(self) -> dict[str, Any]:
        manifest = self._read_json(self.manifest_path)
        previous = manifest.get("previous_runtime")
        if not isinstance(previous, dict) or not Path(str(previous.get("binary") or "")).is_file():
            raise SetupError("no_rollback", "No previous managed llama.cpp runtime is available")
        current = manifest.get("runtime")
        manifest["runtime"] = previous
        manifest["previous_runtime"] = current
        manifest["updated_at"] = time.time()
        self._atomic_json(self.manifest_path, manifest)
        return previous
