"""First-run setup, discovery, and managed local runtime helpers."""

from .engine import SetupEngine, SetupError
from .hardware import collect_hardware_profile

__all__ = ["SetupEngine", "SetupError", "collect_hardware_profile"]
