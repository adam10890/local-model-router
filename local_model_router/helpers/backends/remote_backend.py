"""
Remote Backend — communicates with pre-running llama-server containers via HTTP.

Unlike DockerBackend (which creates/destroys containers) or SubprocessBackend
(which spawns processes), this backend assumes the LMM containers are managed
externally (e.g. by docker-compose.lmm.yml). The plugin only monitors health
and routes requests.

Config keys (in global section of llama_cpp_servers.yaml):
  lmm_hosts:            dict mapping slot role → hostname:port
  health_check_interval: int  — seconds between health probes
  startup_timeout:       int  — seconds to wait for initial health
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from .base import BackendType, InferenceBackend, SlotStatus

logger = logging.getLogger("lmm.backend.remote")

# Default hostnames matching docker-compose.lmm.yml service names
DEFAULT_HOSTS = {
    "chat": "a0-llama-chat:8080",
    "utility": "a0-llama-utility:8088",
    "embedding": "a0-llama-embed:8082",
}


class RemoteBackend(InferenceBackend):
    """
    HTTP-only backend for pre-running LMM containers.

    Does not manage container lifecycle — only monitors health and
    provides endpoint resolution for the plugin's API/tools.
    """

    def __init__(self, global_config: Dict[str, Any]):
        super().__init__(global_config)
        self._slots: Dict[str, SlotStatus] = {}

        # Build host map from config or defaults
        self._hosts = self._build_host_map(global_config)
        logger.info(
            f"RemoteBackend initialized with {len(self._hosts)} host(s): "
            + ", ".join(f"{k}={v}" for k, v in self._hosts.items())
        )

    @property
    def backend_type(self) -> BackendType:
        return BackendType.REMOTE

    # ── Public API ──────────────────────────────────────────────────

    async def start_slot(self, name: str, config: Dict[str, Any]) -> SlotStatus:
        """
        'Start' a remote slot = register it and verify it's reachable.
        The actual container is already running via docker-compose.
        """
        role = config.get("role", "chat")
        host_key = config.get("remote_host_key", role)
        host = self._hosts.get(host_key)

        if not host:
            # Try constructing from config
            container_host = config.get("remote_host", "")
            port = config.get("port", 8080)
            if container_host:
                host = f"{container_host}:{port}"
            else:
                status = SlotStatus(
                    name=name,
                    port=int(port),
                    host="",
                    model_id=config.get("model_id", ""),
                    error=f"No remote host configured for slot role '{role}'",
                    extra={"role": role},
                )
                self._slots[name] = status
                logger.warning(status.error)
                return status

        hostname, port = self._parse_host(host)

        status = SlotStatus(
            name=name,
            port=port,
            host=hostname,
            model_id=config.get("model_id", ""),
            extra={"role": role},
        )

        # Probe health
        timeout = self._get_startup_timeout()
        logger.info(f"Probing remote slot '{name}' at {host} (timeout={timeout}s)")

        if await self._probe_health(hostname, port, timeout):
            status.running = True
            status.healthy = True
            logger.info(f"Remote slot '{name}' is healthy at {host}")
        else:
            status.running = False
            status.healthy = False
            status.error = f"Remote slot not reachable at {host}"
            logger.warning(f"Remote slot '{name}' not reachable at {host}")

        self._slots[name] = status
        return status

    async def stop_slot(self, name: str) -> bool:
        """
        'Stop' a remote slot = remove from tracking.
        Does NOT stop the actual container — that's managed by docker-compose.
        """
        if name in self._slots:
            self._slots[name].running = False
            self._slots[name].healthy = False
            logger.info(f"Unregistered remote slot '{name}' (container still running)")
            return True
        return False

    async def health_check(self, name: str) -> SlotStatus:
        """Check health of a remote slot via HTTP."""
        status = self._slots.get(name)
        if not status:
            return SlotStatus(name=name, error="Unknown slot")

        try:
            healthy = await self._single_health_probe(status.host, status.port)
            status.running = healthy
            status.healthy = healthy
            if not healthy:
                status.error = "Health check failed"
            else:
                status.error = None
        except Exception as e:
            status.running = False
            status.healthy = False
            status.error = str(e)

        return status

    async def list_slots(self) -> Dict[str, SlotStatus]:
        """List all tracked remote slots."""
        return dict(self._slots)

    async def cleanup(self) -> None:
        """Clear tracking. Does not stop any containers."""
        self._slots.clear()
        logger.info("RemoteBackend: cleared all slot tracking")

    # ── Extra: endpoint resolution ──────────────────────────────────

    def get_endpoint(self, slot_name: str) -> Optional[str]:
        """Get the OpenAI-compatible base URL for a slot."""
        status = self._slots.get(slot_name)
        if status and status.healthy:
            return f"http://{status.host}:{status.port}/v1"
        return None

    def get_endpoint_by_role(self, role: str) -> Optional[str]:
        """Get endpoint by role (chat, utility, embedding)."""
        for name, status in self._slots.items():
            if status.healthy and status.extra.get("role") == role:
                return f"http://{status.host}:{status.port}/v1"
        # Fallback: try direct host map
        host = self._hosts.get(role)
        if host:
            hostname, port = self._parse_host(host)
            return f"http://{hostname}:{port}/v1"
        return None

    def get_all_endpoints(self) -> Dict[str, str]:
        """Get all healthy endpoints as {slot_name: url}."""
        return {
            name: f"http://{s.host}:{s.port}/v1"
            for name, s in self._slots.items()
            if s.healthy
        }

    # ── Private helpers ─────────────────────────────────────────────

    def _build_host_map(self, config: Dict[str, Any]) -> Dict[str, str]:
        """Build role → host:port map from config."""
        # Explicit lmm_hosts config
        hosts = config.get("lmm_hosts", {})
        if hosts:
            return dict(hosts)

        # Fallback: construct from known compose service names
        result = dict(DEFAULT_HOSTS)

        # Override from docker_network/container naming convention
        network = config.get("docker_network", "")
        if network:
            # Service names on the compose network are the container names
            pass  # defaults already use compose service names

        return result

    @staticmethod
    def _parse_host(host_str: str) -> tuple:
        """Parse 'hostname:port' string."""
        if ":" in host_str:
            parts = host_str.rsplit(":", 1)
            return parts[0], int(parts[1])
        return host_str, 8080

    async def _probe_health(self, hostname: str, port: int, timeout: int) -> bool:
        """Wait up to `timeout` seconds for a healthy response."""
        import aiohttp

        start = time.time()
        url = f"http://{hostname}:{port}/health"

        while time.time() - start < timeout:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("status") == "ok":
                                return True
            except Exception:
                pass
            await asyncio.sleep(3)

        return False

    async def _single_health_probe(self, hostname: str, port: int) -> bool:
        """Single health check (no retry loop)."""
        import aiohttp

        url = f"http://{hostname}:{port}/health"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("status") == "ok"
        except Exception:
            return False
        return False
