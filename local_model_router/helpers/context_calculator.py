"""Context utilization tiers and lightweight GGUF metadata reading."""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("local_model_router.context_calculator")

# context_planner.DEFAULT_EFFECTIVE_CTX_RATIO â€” history is bounded to ~70% of
# the window, leaving headroom for the system prompt and the response.
EFFECTIVE_CTX_RATIO = 0.70

# Inclusive upper bound of each zone, as a fraction of the full window.
_ZONE_GREEN_MAX = 0.50
_ZONE_YELLOW_MAX = 0.70
_ZONE_ORANGE_MAX = 0.85


def utilization_zone(used: int, total: int) -> str:
    """Map context utilization to a capability zone.

    - ``green``  (<=50%): full capability
    - ``yellow`` (<=70%): good, early degradation
    - ``orange`` (<=85%): noticeable degradation â€” compress or route to a
      larger window
    - ``red``    (>85%):  risk to output quality

    Returns ``"unknown"`` when the window size is not known.
    """
    if total <= 0:
        return "unknown"
    frac = used / total
    if frac <= _ZONE_GREEN_MAX:
        return "green"
    if frac <= _ZONE_YELLOW_MAX:
        return "yellow"
    if frac <= _ZONE_ORANGE_MAX:
        return "orange"
    return "red"


@dataclass
class ContextUtilization:
    """A request's estimated context utilization against a slot's window."""

    used: int
    total: int

    @property
    def percent(self) -> float:
        if self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def zone(self) -> str:
        return utilization_zone(self.used, self.total)

    @property
    def effective_budget(self) -> int:
        """Tokens allowed before the request is considered over budget."""
        return int(self.total * EFFECTIVE_CTX_RATIO)

    @property
    def over_budget(self) -> bool:
        """True when usage exceeds the effective (history) budget."""
        return self.total > 0 and self.used > self.effective_budget

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.used)


def read_gguf_metadata(model_path: str) -> dict:
    """
    Read GGUF metadata to extract context-related information.

    Returns dict with:
        n_ctx_train: max training context length
        n_embd: embedding dimension
        n_layer: number of layers
        n_head: number of attention heads
        file_size_gb: model file size in GB
    """
    path = Path(model_path)
    if not path.exists():
        log.warning(f"Model file not found: {model_path}")
        return {}

    file_size_gb = path.stat().st_size / (1024**3)

    try:
        # Simple GGUF reader - reads key-value pairs from the file
        # GGUF format: magic (4 bytes) + version (4 bytes) + tensor_count (8 bytes) + KV count (8 bytes)
        # Then KV pairs: key_len (8 bytes) + key_str + value_type (1 byte) + value_data

        metadata = {
            "n_ctx_train": None,
            "n_embd": None,
            "n_layer": None,
            "n_head": None,
            "file_size_gb": file_size_gb,
        }

        with open(path, "rb") as f:
            # Read header
            magic = f.read(4)
            if magic != b"GGUF":
                log.warning(f"Not a GGUF file: {model_path}")
                return metadata

            version = struct.unpack("<I", f.read(4))[0]
            tensor_count = struct.unpack("<Q", f.read(8))[0]
            kv_count = struct.unpack("<Q", f.read(8))[0]

            # Read KV pairs
            for _ in range(kv_count):
                key_len = struct.unpack("<Q", f.read(8))[0]
                key = f.read(key_len).decode("utf-8", errors="ignore")
                value_type = struct.unpack("<I", f.read(4))[0]

                # Read value based on type
                if value_type == 0:  # UINT8
                    value = struct.unpack("<B", f.read(1))[0]
                elif value_type == 1:  # INT8
                    value = struct.unpack("<b", f.read(1))[0]
                elif value_type == 2:  # UINT16
                    value = struct.unpack("<H", f.read(2))[0]
                elif value_type == 3:  # INT16
                    value = struct.unpack("<h", f.read(2))[0]
                elif value_type == 4:  # UINT32
                    value = struct.unpack("<I", f.read(4))[0]
                elif value_type == 5:  # INT32
                    value = struct.unpack("<i", f.read(4))[0]
                elif value_type == 6:  # FLOAT32
                    value = struct.unpack("<f", f.read(4))[0]
                elif value_type == 7:  # BOOL
                    value = struct.unpack("<?", f.read(1))[0]
                elif value_type == 8:  # STRING
                    str_len = struct.unpack("<Q", f.read(8))[0]
                    value = f.read(str_len).decode("utf-8", errors="ignore")
                elif value_type == 9:  # ARRAY
                    arr_type = struct.unpack("<I", f.read(4))[0]
                    arr_len = struct.unpack("<Q", f.read(8))[0]
                    if arr_type == 4:  # UINT32 array
                        value = [struct.unpack("<I", f.read(4))[0] for _ in range(arr_len)]
                    elif arr_type == 5:  # INT32 array
                        value = [struct.unpack("<i", f.read(4))[0] for _ in range(arr_len)]
                    elif arr_type == 6:  # FLOAT32 array
                        value = [struct.unpack("<f", f.read(4))[0] for _ in range(arr_len)]
                    else:
                        # Skip unknown array types
                        for _ in range(arr_len):
                            if arr_type in (0, 1, 7):
                                f.read(1)
                            elif arr_type in (2, 3):
                                f.read(2)
                            elif arr_type in (4, 5, 6):
                                f.read(4)
                            elif arr_type in (10, 11, 12):
                                f.read(8)
                            elif arr_type == 8:
                                str_len = struct.unpack("<Q", f.read(8))[0]
                                f.read(str_len)
                            else:
                                break
                        value = None
                elif value_type == 10:  # UINT64
                    value = struct.unpack("<Q", f.read(8))[0]
                elif value_type == 11:  # INT64
                    value = struct.unpack("<q", f.read(8))[0]
                elif value_type == 12:  # FLOAT64
                    value = struct.unpack("<d", f.read(8))[0]
                else:
                    # Unknown scalar size; stop rather than desynchronizing the stream.
                    break

                # Extract relevant metadata
                if key == "n_ctx_train" or key.endswith(".context_length"):
                    metadata["n_ctx_train"] = int(value) if isinstance(value, (int, str)) else None
                elif key == "n_embd" or key.endswith(".embedding_length"):
                    metadata["n_embd"] = int(value) if isinstance(value, (int, str)) else None
                elif key == "n_layer" or key.endswith(".block_count"):
                    metadata["n_layer"] = int(value) if isinstance(value, (int, str)) else None
                elif key == "n_head" or key.endswith(".attention.head_count"):
                    metadata["n_head"] = int(value) if isinstance(value, (int, str)) else None

        return metadata

    except Exception as e:
        log.error(f"Failed to read GGUF metadata from {model_path}: {e}")
        return {"file_size_gb": file_size_gb}
