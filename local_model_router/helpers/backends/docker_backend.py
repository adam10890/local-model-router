"""
Docker Backend — runs each llama-server slot as a GPU-enabled container.

Requirements:
  - Docker Engine accessible (socket at /var/run/docker.sock or DOCKER_HOST)
  - NVIDIA Container Toolkit installed on host
  - Models directory mounted from host

Container image: ghcr.io/ggml-org/llama.cpp:server-cuda (official, ~2GB)
Fallback images: ghcr.io/ggml-org/llama.cpp:server (CPU-only)

Each slot = one container. Multiple slots = parallel model execution.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from .base import BackendType, InferenceBackend, SlotStatus

# Docker SDK is optional — imported lazily
_docker = None
_docker_errors = None


def _ensure_docker():
    """Lazy-import docker SDK."""
    global _docker, _docker_errors
    if _docker is None:
        try:
            import docker
            import docker.errors
            _docker = docker
            _docker_errors = docker.errors
        except ImportError as exc:
            raise ImportError(
                "Docker SDK not installed. Run: pip install docker\n"
                "Or switch backend to 'subprocess' in llama_cpp_servers.yaml"
            ) from exc


# ── Default config ──────────────────────────────────────────────────

DEFAULT_IMAGE_GPU = "ghcr.io/ggml-org/llama.cpp:server-cuda"
DEFAULT_IMAGE_CPU = "ghcr.io/ggml-org/llama.cpp:server"
DEFAULT_NETWORK = "a0-lmm-net"
CONTAINER_MODELS_DIR = "/models"
CONTAINER_PREFIX = "a0-lmm-"


class DockerBackend(InferenceBackend):
    """
    Manages llama-server instances as Docker containers.

    Config keys (in global section of llama_cpp_servers.yaml):
      docker_image_gpu: str   — GPU image (default: server-cuda)
      docker_image_cpu: str   — CPU image (default: server)
      docker_network: str     — network name (default: a0-lmm-net)
      models_dir: str         — host path to GGUF models
      gpu_count: int|str      — GPUs to allocate (default: "all")
    """

    def __init__(self, global_config: Dict[str, Any]):
        super().__init__(global_config)
        _ensure_docker()
        self._client = _docker.from_env()
        self._slots: Dict[str, SlotStatus] = {}
        self._containers: Dict[str, Any] = {}  # name → Container object
        self._ensure_network()

    @property
    def backend_type(self) -> BackendType:
        return BackendType.DOCKER

    # ── Public API ──────────────────────────────────────────────────

    async def start_slot(self, name: str, config: Dict[str, Any]) -> SlotStatus:
        """Start a llama-server container for this slot."""
        if name in self._containers:
            existing = self._containers[name]
            existing.reload()
            if existing.status == "running":
                self.logger.info(f"Slot '{name}' already running")
                return self._slots[name]
            # Remove stale container
            await self._remove_container(name)

        port = int(config.get("port", 8080))
        gpu_layers = config.get("gpu_layers", -1)
        use_gpu = gpu_layers != 0

        image = self._get_image(use_gpu)
        cmd = self._build_container_cmd(config)
        models_dir = self._get_models_dir()

        self.logger.info(
            f"Starting slot '{name}' — image={image}, port={port}, "
            f"gpu={'yes' if use_gpu else 'no'}, models={models_dir}"
        )

        status = SlotStatus(
            name=name,
            port=port,
            host="localhost",
            model_id=config.get("model_id", ""),
        )

        try:
            volumes = {models_dir: {"bind": CONTAINER_MODELS_DIR, "mode": "ro"}}

            # GPU configuration
            device_requests = []
            if use_gpu:
                gpu_count = self.global_config.get("gpu_count", "all")
                if gpu_count == "all":
                    device_requests = [
                        _docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
                    ]
                else:
                    device_requests = [
                        _docker.types.DeviceRequest(
                            count=int(gpu_count), capabilities=[["gpu"]]
                        )
                    ]

            container = self._client.containers.run(
                image,
                command=cmd,
                name=f"{CONTAINER_PREFIX}{name}",
                detach=True,
                ports={f"{port}/tcp": port},
                volumes=volumes,
                device_requests=device_requests if device_requests else None,
                network=self._get_network_name(),
                environment=self._build_env(config),
                restart_policy={"Name": "unless-stopped"},
                labels={"a0.lmm.slot": name, "a0.lmm.managed": "true"},
            )

            self._containers[name] = container
            status.container_id = container.id[:12]

            # Wait for health
            timeout = self._get_startup_timeout()
            if await self._wait_healthy(port, timeout):
                status.running = True
                status.healthy = True
                self.logger.info(f"Slot '{name}' ready on port {port}")
            else:
                status.error = "Container started but health check failed"
                self.logger.error(f"Slot '{name}' failed health check")
                # Don't stop — might just be slow model loading

        except Exception as e:
            status.error = str(e)
            self.logger.error(f"Failed to start slot '{name}': {e}")

        self._slots[name] = status
        return status

    async def stop_slot(self, name: str) -> bool:
        return await self._remove_container(name)

    async def health_check(self, name: str) -> SlotStatus:
        status = self._slots.get(name)
        if not status:
            return SlotStatus(name=name, error="Unknown slot")

        container = self._containers.get(name)
        if not container:
            status.running = False
            status.healthy = False
            return status

        try:
            container.reload()
            status.running = container.status == "running"

            if status.running:
                import aiohttp
                try:
                    async with aiohttp.ClientSession() as session:
                        url = f"http://localhost:{status.port}/health"
                        async with session.get(url, timeout=5) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                status.healthy = data.get("status") == "ok"
                            else:
                                status.healthy = False
                except Exception:
                    status.healthy = False
            else:
                status.healthy = False

        except Exception as e:
            status.error = str(e)
            status.running = False
            status.healthy = False

        return status

    async def list_slots(self) -> Dict[str, SlotStatus]:
        # Also discover any orphaned a0-lmm containers
        try:
            containers = self._client.containers.list(
                all=True,
                filters={"label": "a0.lmm.managed=true"},
            )
            for c in containers:
                slot_name = c.labels.get("a0.lmm.slot", "")
                if slot_name and slot_name not in self._slots:
                    self._containers[slot_name] = c
                    self._slots[slot_name] = SlotStatus(
                        name=slot_name,
                        running=c.status == "running",
                        container_id=c.id[:12],
                    )
        except Exception as e:
            self.logger.warning(f"Failed to list containers: {e}")

        return dict(self._slots)

    async def start_ephemeral_slot(
        self, name: str, config: Dict[str, Any]
    ) -> SlotStatus:
        """Start a one-time ephemeral container for a single conversation.

        Identical to start_slot but uses the ephemeral label set so the pool
        and cleanup_stale_ephemerals can identify these containers separately
        from long-lived shared slots.
        """
        if name in self._containers:
            existing = self._containers[name]
            existing.reload()
            if existing.status == "running":
                return self._slots[name]
            await self._remove_container(name)

        port = int(config.get("port", 9100))
        gpu_layers = config.get("gpu_layers", -1)
        use_gpu = gpu_layers != 0
        image = self._get_image(use_gpu)
        cmd = self._build_container_cmd(config)
        models_dir = self._get_models_dir()

        self.logger.info(
            f"Starting ephemeral slot '{name}' — ctx={config.get('context_size')}, "
            f"port={port}, gpu={'yes' if use_gpu else 'no'}"
        )

        status = SlotStatus(
            name=name,
            port=port,
            host="localhost",
            model_id=config.get("model_id", ""),
        )

        try:
            volumes = {models_dir: {"bind": CONTAINER_MODELS_DIR, "mode": "ro"}}
            device_requests = []
            if use_gpu:
                gpu_count = self.global_config.get("gpu_count", "all")
                if gpu_count == "all":
                    device_requests = [
                        _docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
                    ]
                else:
                    device_requests = [
                        _docker.types.DeviceRequest(
                            count=int(gpu_count), capabilities=[["gpu"]]
                        )
                    ]

            container = self._client.containers.run(
                image,
                command=cmd,
                name=name,
                detach=True,
                ports={f"{port}/tcp": port},
                volumes=volumes,
                device_requests=device_requests if device_requests else None,
                network=self._get_network_name(),
                environment=self._build_env(config),
                labels={
                    "a0.lmm.slot": name,
                    "a0.lmm.managed": "true",
                    "a0.lmm.ephemeral": "true",
                },
            )

            self._containers[name] = container
            status.container_id = container.id[:12]

            timeout = self._get_startup_timeout()
            if await self._wait_healthy(port, timeout):
                status.running = True
                status.healthy = True
                self.logger.info(f"Ephemeral slot '{name}' ready on port {port}")
            else:
                status.error = "Container started but health check timed out"

        except Exception as e:
            status.error = str(e)
            self.logger.error(f"Failed to start ephemeral slot '{name}': {e}")

        self._slots[name] = status
        return status

    async def stop_ephemeral_slot(self, name: str) -> bool:
        """Stop and remove an ephemeral container by name."""
        return await self._remove_container(name)

    async def cleanup_stale_ephemerals(self, ttl_hours: float = 24.0) -> int:
        """Find ephemeral containers older than ttl_hours and destroy them.

        Useful as a periodic background task to reclaim VRAM from abandoned
        conversations (e.g. after agent crash or client disconnect).
        """
        import datetime

        destroyed = 0
        try:
            containers = self._client.containers.list(
                all=True,
                filters={"label": "a0.lmm.ephemeral=true"},
            )
            cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=ttl_hours)
            for c in containers:
                created_str = c.attrs.get("Created", "")
                try:
                    created = datetime.datetime.fromisoformat(
                        created_str.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                    if created < cutoff:
                        name = c.name
                        c.stop(timeout=5)
                        c.remove(force=True)
                        self._containers.pop(name, None)
                        self._slots.pop(name, None)
                        self.logger.info(f"Removed stale ephemeral container: {name}")
                        destroyed += 1
                except Exception:
                    pass
        except Exception as e:
            self.logger.warning(f"cleanup_stale_ephemerals error: {e}")

        return destroyed

    async def cleanup(self) -> None:
        """Stop all managed containers."""
        names = list(self._containers.keys())
        for name in names:
            await self._remove_container(name)
        self.logger.info(f"Cleaned up {len(names)} container(s)")

    # ── Private helpers ─────────────────────────────────────────────

    def _get_image(self, use_gpu: bool) -> str:
        if use_gpu:
            return self.global_config.get("docker_image_gpu", DEFAULT_IMAGE_GPU)
        return self.global_config.get("docker_image_cpu", DEFAULT_IMAGE_CPU)

    def _get_network_name(self) -> str:
        return self.global_config.get("docker_network", DEFAULT_NETWORK)

    def _ensure_network(self) -> None:
        """Create Docker network if it doesn't exist."""
        net_name = self._get_network_name()
        try:
            self._client.networks.get(net_name)
        except _docker_errors.NotFound:
            self._client.networks.create(net_name, driver="bridge")
            self.logger.info(f"Created Docker network: {net_name}")

    def _build_container_cmd(self, config: Dict[str, Any]) -> List[str]:
        """Build llama-server command line for container execution."""
        port = int(config.get("port", 8080))
        ctx_size = int(config.get("context_size", 8192))
        batch_size = int(config.get("batch_size", 512))
        threads = int(config.get("threads", 4))
        parallel = int(config.get("parallel_slots", 1))
        gpu_layers = config.get("gpu_layers", -1)

        cmd = [
            "--ctx-size", str(ctx_size),
            "--batch-size", str(batch_size),
            "--threads", str(threads),
            "--parallel", str(parallel),
            "--port", str(port),
            "--host", "0.0.0.0",
        ]

        if config.get("router_mode"):
            # ── Router Mode: directory-based hot-swap ─────────────────
            rdir = config.get("router_models_dir", "") or CONTAINER_MODELS_DIR
            preset = config.get("router_models_preset", "")
            if rdir and not preset:
                cmd.extend(["--models-dir", rdir])
            if config.get("router_models_autoload", True):
                cmd.append("--models-autoload")
            if preset:
                cmd.extend(["--models-preset", preset])
            rmax = int(config.get("router_models_max", 1))
            if rmax > 0:
                cmd.extend(["--models-max", str(rmax)])
            # Pre-load default model to avoid first-request swap latency
            default_alias = config.get("router_default_model", "")
            if default_alias and preset:
                from local_model_router.helpers.llama_cpp_manager import resolve_preset_alias  # noqa: PLC0415
                path = resolve_preset_alias(preset, default_alias, rdir)
                if path:
                    cmd.extend(["--model", path])
        else:
            # ── Single-model mode (default) ────────────────────────────
            model_path = config.get("model_path", "")
            models_dir = self._get_models_dir()
            if model_path.startswith(models_dir):
                rel_path = model_path[len(models_dir):].lstrip("/\\")
            else:
                rel_path = model_path
            container_model_path = f"{CONTAINER_MODELS_DIR}/{rel_path}"
            cmd = ["--model", container_model_path] + cmd

        if gpu_layers != 0:
            cmd.extend(["--n-gpu-layers", str(gpu_layers)])

        # Flash attention
        fa = config.get("flash_attention")
        if fa is True:
            cmd.extend(["--flash-attn", "on"])
        elif fa is False:
            cmd.extend(["--flash-attn", "off"])

        # Auto-fit VRAM
        if config.get("fit", True):
            cmd.extend(["--fit", "on"])
            fit_target = config.get("fit_target_mib", 1024)
            if fit_target > 0:
                cmd.extend(["--fit-target", str(fit_target)])

        # Embedding mode
        if config.get("embedding_mode", False):
            cmd.append("--embedding")

        # Reasoning
        rf = config.get("reasoning_format", "")
        if rf:
            cmd.extend(["--reasoning-format", rf])

        # Jinja
        if config.get("jinja") is False:
            cmd.append("--no-jinja")

        # Multimodal vision projector
        mmproj = config.get("mmproj_path", "")
        if mmproj:
            cmd.extend(["--mmproj", f"{CONTAINER_MODELS_DIR}/{mmproj}"])

        # Speculative decoding — spec-type (MTP, ngram, or draft-model-based)
        # For MTP: set spec_type="draft-mtp" — no draft model needed (heads are in the model).
        # For draft-simple / draft-eagle3: also set draft_model_path.
        # For ngram variants: set spec_type only — no model needed.
        spec_type = config.get("spec_type", "")
        if spec_type:
            cmd.extend(["--spec-type", spec_type])
            # draft_max controls --spec-draft-n-max (sweet spot for MTP is 2)
            draft_max = int(config.get("draft_max", 0) or 0)
            if draft_max > 0:
                cmd.extend(["--spec-draft-n-max", str(draft_max)])
            draft_min = int(config.get("draft_min", 0) or 0)
            if draft_min > 0:
                cmd.extend(["--spec-draft-n-min", str(draft_min)])
            draft_p_min = float(config.get("draft_p_min", 0.0) or 0.0)
            if draft_p_min > 0.0:
                cmd.extend(["--spec-draft-p-min", str(draft_p_min)])

        # External draft model (draft-simple / draft-eagle3 / legacy speculative decoding)
        draft = config.get("draft_model_path", "")
        if draft:
            cmd.extend(["--spec-draft-model", f"{CONTAINER_MODELS_DIR}/{draft}"])
            # Only add tuning flags here if spec_type wasn't already set above
            if not spec_type:
                draft_max = int(config.get("draft_max", 0) or 0)
                if draft_max > 0:
                    cmd.extend(["--spec-draft-n-max", str(draft_max)])
                draft_min = int(config.get("draft_min", 0) or 0)
                if draft_min > 0:
                    cmd.extend(["--spec-draft-n-min", str(draft_min)])
                draft_p_min = float(config.get("draft_p_min", 0.0) or 0.0)
                if draft_p_min > 0.0:
                    cmd.extend(["--spec-draft-p-min", str(draft_p_min)])

        # ── Optimization flags (opt-in, all default off) ─────────────
        # NOTE: active backend is "remote" (compose-managed containers).
        # These flags are inert until backend is switched to "docker".

        if config.get("no_mmap"):
            cmd.append("--no-mmap")

        if config.get("mlock"):
            cmd.append("--mlock")

        cpu_moe = config.get("cpu_moe")
        if cpu_moe is not None and cpu_moe > 0:
            cmd.extend(["--n-cpu-moe", str(cpu_moe)])

        cache_type_k = config.get("cache_type_k")
        if cache_type_k:
            cmd.extend(["--cache-type-k", str(cache_type_k)])

        cache_type_v = config.get("cache_type_v")
        if cache_type_v:
            cmd.extend(["--cache-type-v", str(cache_type_v)])

        return cmd

    def _build_env(self, config: Dict[str, Any]) -> Dict[str, str]:
        """Environment variables for the container."""
        env = {}
        cuda_devices = self.global_config.get("cuda_visible_devices")
        if cuda_devices and cuda_devices != "auto":
            env["CUDA_VISIBLE_DEVICES"] = str(cuda_devices)
        return env

    async def _remove_container(self, name: str) -> bool:
        """Stop and remove a container."""
        container = self._containers.pop(name, None)
        self._slots.pop(name, None)

        if not container:
            return False

        try:
            container.reload()
            if container.status in ("running", "restarting"):
                container.stop(timeout=10)
            container.remove(force=True)
            self.logger.info(f"Removed container for slot '{name}'")
            return True
        except Exception as e:
            self.logger.warning(f"Error removing container '{name}': {e}")
            return False

    async def _wait_healthy(self, port: int, timeout: int) -> bool:
        """Wait for the /health endpoint to return ok."""
        import aiohttp

        start = time.time()
        while time.time() - start < timeout:
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"http://localhost:{port}/health"
                    async with session.get(url, timeout=5) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("status") == "ok":
                                return True
            except Exception:
                pass
            await asyncio.sleep(3)
        return False


# ── Utility: check Docker availability ──────────────────────────────

def is_docker_available() -> bool:
    """Check if Docker daemon is accessible."""
    try:
        _ensure_docker()
        client = _docker.from_env()
        client.ping()
        return True
    except Exception:
        return False
