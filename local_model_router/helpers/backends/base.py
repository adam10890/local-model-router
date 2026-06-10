"""
Abstract base class for LMM inference backends.

Each backend knows how to start/stop/health-check a llama-server instance,
whether that instance lives in a Docker container, a subprocess, or an
external Ollama service.
"""
from __future__ import annotations

import enum
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class BackendType(enum.Enum):
    SUBPROCESS = "subprocess"
    DOCKER = "docker"
    REMOTE = "remote"


@dataclass
class SlotStatus:
    """Runtime status of a single inference slot."""
    name: str
    running: bool = False
    healthy: bool = False
    port: int = 0
    host: str = "localhost"
    pid: Optional[int] = None
    container_id: Optional[str] = None
    error: Optional[str] = None
    model_id: str = ""
    vram_mb: int = 0
    uptime_s: float = 0.0
    restart_count: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class InferenceBackend(ABC):
    """
    Abstract interface for starting / stopping llama-server instances.

    All concrete backends must implement:
      - start_slot(name, config)   → SlotStatus
      - stop_slot(name)            → bool
      - health_check(name)         → SlotStatus
      - list_slots()               → dict[name, SlotStatus]
      - cleanup()                  → None  (stop everything)
    """

    def __init__(self, global_config: Dict[str, Any]):
        self.global_config = global_config
        self.logger = logging.getLogger(f"lmm.backend.{self.backend_type.value}")

    @property
    @abstractmethod
    def backend_type(self) -> BackendType:
        ...

    @abstractmethod
    async def start_slot(self, name: str, config: Dict[str, Any]) -> SlotStatus:
        """Start an inference slot. Returns SlotStatus."""
        ...

    @abstractmethod
    async def stop_slot(self, name: str) -> bool:
        """Stop an inference slot. Returns True if stopped successfully."""
        ...

    @abstractmethod
    async def health_check(self, name: str) -> SlotStatus:
        """Check health of a running slot."""
        ...

    @abstractmethod
    async def list_slots(self) -> Dict[str, SlotStatus]:
        """List all managed slots and their status."""
        ...

    @abstractmethod
    async def cleanup(self) -> None:
        """Stop all slots and free resources."""
        ...

    # ── Shared helpers ──────────────────────────────────────────────

    def _get_models_dir(self) -> str:
        """Get the configured models directory."""
        return self.global_config.get("models_dir", "/models")

    def _get_startup_timeout(self) -> int:
        return int(self.global_config.get("startup_timeout", 180))

    def _get_health_interval(self) -> int:
        return int(self.global_config.get("health_check_interval", 30))
