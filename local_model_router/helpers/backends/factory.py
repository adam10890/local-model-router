"""
Backend factory — auto-detects environment and creates the right backend.

``auto`` is deliberately native-first: remote when hosts are configured,
otherwise subprocess. Docker is loaded only when explicitly configured.
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

    logger.info("No lmm_hosts configured — using native subprocess backend")
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
