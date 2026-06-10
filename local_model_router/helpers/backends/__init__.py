"""
LMM Inference Backends

Auto-detects Docker availability and falls back to subprocess.
"""
from .base import InferenceBackend, BackendType
from .factory import create_backend, detect_backend

__all__ = [
    "InferenceBackend",
    "BackendType",
    "create_backend",
    "detect_backend",
]
