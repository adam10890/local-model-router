"""
Backend factory — auto-detects environment and creates the right backend.

Detection order:
  1. If config says backend: remote     → RemoteBackend (HTTP to pre-running containers)
  2. If config says backend: docker     → DockerBackend (manages containers via Docker SDK)
  3. If config says backend: subprocess → SubprocessBackend (local llama-server processes)
  4. If config says backend: auto (default):
     a. Check if lmm_hosts configured  → RemoteBackend
     b. Check Docker socket available   → DockerBackend
     c. Fallback                        → SubprocessBackend
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from .base import BackendType, InferenceBackend

logger = logging.getLogger("lmm.backend.factory")


def detect_backend(global_config: Dict[str, Any] | None = None) -> BackendType:
    """Auto-detect the best available backend."""
    global_config = global_config or {}

    # If lmm_hosts is configured, prefer remote
    if global_config.get("lmm_hosts"):
        logger.info("lmm_hosts configured — using remote backend")
        return BackendType.REMOTE

    # Check Docker availability
    try:
        from .docker_backend import is_docker_available
        if is_docker_available():
            logger.info("Docker available — using docker backend")
            return BackendType.DOCKER
    except Exception:
        pass

    # Fallback to subprocess
    logger.info("No Docker or lmm_hosts — using subprocess backend")
    return BackendType.SUBPROCESS


def create_backend(
    global_config: Dict[str, Any],
    backend_type: BackendType | str | None = None,
) -> InferenceBackend:
    """
    Create an inference backend instance.

    Args:
        global_config: The 'global' section from llama_cpp_servers.yaml
        backend_type: "auto", "remote", "docker", "subprocess", or BackendType enum.
                      If None, reads from global_config["backend"].
    """
    # Resolve backend type
    if backend_type is None:
        backend_type = global_config.get("backend", "auto")

    if isinstance(backend_type, str):
        backend_type = backend_type.lower().strip()
        if backend_type == "auto":
            backend_type = detect_backend(global_config)
        elif backend_type == "remote":
            backend_type = BackendType.REMOTE
        elif backend_type == "docker":
            backend_type = BackendType.DOCKER
        elif backend_type == "subprocess":
            backend_type = BackendType.SUBPROCESS
        else:
            logger.warning(f"Unknown backend '{backend_type}', falling back to auto")
            backend_type = detect_backend(global_config)

    # Create backend
    if backend_type == BackendType.REMOTE:
        from .remote_backend import RemoteBackend
        return RemoteBackend(global_config)
    elif backend_type == BackendType.DOCKER:
        from .docker_backend import DockerBackend
        return DockerBackend(global_config)
    else:
        from .subprocess_backend import SubprocessBackend
        return SubprocessBackend(global_config)
