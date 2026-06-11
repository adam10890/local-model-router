"""Minimal GGUF header reader: metadata key-values only, no tensor data.

Reads just enough of a .gguf file to answer the Cookbook's questions:
architecture, layer/head geometry (for KV-cache math), training context,
parameter count, and quantization. Stops at the tokenizer block — the
needed keys always precede it — so even 40 GB files cost a few KB of I/O.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

GGUF_MAGIC = b"GGUF"

# value-type id -> (struct format, byte size) for fixed-size scalars
_SCALARS: Dict[int, tuple] = {
    0: ("<B", 1),   # uint8
    1: ("<b", 1),   # int8
    2: ("<H", 2),   # uint16
    3: ("<h", 2),   # int16
    4: ("<I", 4),   # uint32
    5: ("<i", 4),   # int32
    6: ("<f", 4),   # float32
    7: ("<B", 1),   # bool
    10: ("<Q", 8),  # uint64
    11: ("<q", 8),  # int64
    12: ("<d", 8),  # float64
}
_TYPE_STRING = 8
_TYPE_ARRAY = 9

_MAX_STRING = 1_000_000
_MAX_KV = 4096

# llama.cpp general.file_type -> quant label (subset that appears in the wild)
FILE_TYPES: Dict[int, str] = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0",
    9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L",
    14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K",
    19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS",
    24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S",
    29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
}

# effective bits per weight, used to estimate parameter count from file size
QUANT_BITS: Dict[str, float] = {
    "F32": 32.0, "BF16": 16.0, "F16": 16.0,
    "Q8_0": 8.5, "Q6_K": 6.6, "Q5_K_M": 5.7, "Q5_K_S": 5.5, "Q5_1": 6.0,
    "Q5_0": 5.5, "Q4_K_M": 4.85, "Q4_K_S": 4.6, "Q4_1": 5.0, "Q4_0": 4.55,
    "Q3_K_L": 4.0, "Q3_K_M": 3.9, "Q3_K_S": 3.5, "Q2_K": 3.35, "Q2_K_S": 3.0,
    "IQ4_XS": 4.3, "IQ4_NL": 4.5, "IQ3_M": 3.7, "IQ3_S": 3.5, "IQ3_XS": 3.3,
    "IQ3_XXS": 3.1, "IQ2_M": 2.7, "IQ2_S": 2.5, "IQ2_XS": 2.4, "IQ2_XXS": 2.1,
    "IQ1_M": 1.8, "IQ1_S": 1.6,
}
DEFAULT_BITS = 4.85  # Q4_K_M is the most common quant in local setups

_SHARD_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.IGNORECASE)


@dataclass
class GgufMeta:
    """Parsed GGUF header fields relevant to fit math."""

    architecture: str = ""
    name: str = ""
    quant: str = ""
    parameter_count: Optional[int] = None
    n_layers: Optional[int] = None
    n_embd: Optional[int] = None
    n_head: Optional[int] = None
    n_head_kv: Optional[int] = None
    n_ctx_train: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def head_dim(self) -> Optional[int]:
        if self.n_embd and self.n_head:
            return self.n_embd // self.n_head
        return None

    def kv_bytes_per_token(self, kv_dtype_bytes: int = 2) -> Optional[int]:
        """K + V cache bytes for one token of context (f16 cache by default)."""
        heads_kv = self.n_head_kv or self.n_head
        if not (self.n_layers and heads_kv and self.head_dim):
            return None
        return 2 * self.n_layers * heads_kv * self.head_dim * kv_dtype_bytes


def _read_exact(fh, n: int) -> bytes:
    data = fh.read(n)
    if len(data) != n:
        raise ValueError("truncated GGUF header")
    return data


def _read_u32(fh) -> int:
    return struct.unpack("<I", _read_exact(fh, 4))[0]


def _read_u64(fh) -> int:
    return struct.unpack("<Q", _read_exact(fh, 8))[0]


def _read_string(fh) -> str:
    length = _read_u64(fh)
    if length > _MAX_STRING:
        raise ValueError(f"unreasonable GGUF string length {length}")
    return _read_exact(fh, length).decode("utf-8", errors="replace")


def _skip_string(fh) -> None:
    length = _read_u64(fh)
    if length > 100_000_000:
        raise ValueError(f"unreasonable GGUF string length {length}")
    fh.seek(length, 1)


def _read_value(fh, vtype: int) -> Any:
    if vtype in _SCALARS:
        fmt, size = _SCALARS[vtype]
        value = struct.unpack(fmt, _read_exact(fh, size))[0]
        return bool(value) if vtype == 7 else value
    if vtype == _TYPE_STRING:
        return _read_string(fh)
    if vtype == _TYPE_ARRAY:
        _skip_array(fh)
        return None  # arrays (vocab etc.) are never needed for fit math
    raise ValueError(f"unknown GGUF value type {vtype}")


def _skip_array(fh) -> None:
    etype = _read_u32(fh)
    count = _read_u64(fh)
    if etype in _SCALARS:
        fh.seek(count * _SCALARS[etype][1], 1)
        return
    if etype == _TYPE_STRING:
        for _ in range(count):
            _skip_string(fh)
        return
    raise ValueError(f"unsupported GGUF array element type {etype}")


def read_gguf_meta(path: Path) -> GgufMeta:
    """Parse the metadata header of a GGUF file. Raises ValueError on bad files."""
    kvs: Dict[str, Any] = {}
    with open(path, "rb") as fh:
        if _read_exact(fh, 4) != GGUF_MAGIC:
            raise ValueError("not a GGUF file (bad magic)")
        version = _read_u32(fh)
        if version < 2:  # v1 uses 32-bit counts; nobody ships it anymore
            raise ValueError(f"unsupported GGUF version {version}")
        _read_u64(fh)  # tensor count (unused)
        kv_count = _read_u64(fh)
        if kv_count > _MAX_KV:
            raise ValueError(f"unreasonable GGUF kv count {kv_count}")

        for _ in range(kv_count):
            key = _read_string(fh)
            if key.startswith("tokenizer."):
                break  # geometry keys always precede the tokenizer block
            vtype = _read_u32(fh)
            value = _read_value(fh, vtype)
            if value is not None:
                kvs[key] = value

    arch = str(kvs.get("general.architecture", ""))

    def arch_key(suffix: str) -> Optional[Any]:
        return kvs.get(f"{arch}.{suffix}")

    file_type = kvs.get("general.file_type")
    param_count = kvs.get("general.parameter_count")
    return GgufMeta(
        architecture=arch,
        name=str(kvs.get("general.name", "")),
        quant=FILE_TYPES.get(file_type, "") if file_type is not None else "",
        parameter_count=int(param_count) if param_count else None,
        n_layers=arch_key("block_count"),
        n_embd=arch_key("embedding_length"),
        n_head=arch_key("attention.head_count"),
        n_head_kv=arch_key("attention.head_count_kv"),
        n_ctx_train=arch_key("context_length"),
        raw=kvs,
    )


def shard_info(file_name: str) -> Optional[tuple]:
    """Return (index, total) for multi-part GGUFs like model-00001-of-00003.gguf."""
    match = _SHARD_RE.search(file_name)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def shard_base(file_name: str) -> str:
    """Shared base name for all shards of a multi-part GGUF."""
    return _SHARD_RE.sub("", file_name)


def estimate_params(size_bytes: int, quant: str) -> int:
    """Estimate parameter count from file size and quant bits."""
    bits = QUANT_BITS.get(quant, DEFAULT_BITS)
    return int(size_bytes * 8 / bits)


def quant_from_filename(file_name: str) -> str:
    """Fallback quant detection from common filename conventions."""
    upper = file_name.upper()
    for label in sorted(QUANT_BITS, key=len, reverse=True):
        if label in upper:
            return label
    return ""
