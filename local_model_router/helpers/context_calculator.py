"""
helpers/context_calculator.py — Automatic context window sizing.

Calculates optimal context window size based on:
1. Model's max context from GGUF metadata (n_ctx_train)
2. Available VRAM (dynamic calculation)
3. Model size (smaller model = larger context)
4. Runtime token budget from external sources (pen_paper, wiki)

KV cache formula: ctx_size * 2 * n_layer * n_embd * bytes_per_token
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("a0_lmm_router.context_calculator")

# Minimum context required by Agent Zero system prompts per role
# Based on actual system prompt sizes and typical usage patterns
_MIN_CTX_SIZES = {
    "chat": 16384,      # Full system prompt (~3.3K tokens) + history + tool results. 8K will choke.
    "utility": 8192,    # Shorter prompt + specific task + tool result. 16K is better but 8K is minimum.
    "embedding": 4096,  # Embedding model - minimal context needed
    "vision": 16384,    # Vision tasks need context for image + text
    "reasoning": 16384, # Reasoning tasks need context for complex chains
}

# Default fallback values if metadata reading fails (same as minimums for safety)
_DEFAULT_CTX_SIZES = {
    "chat": 32768,
    "utility": 16384,
    "embedding": 8192,
    "vision": 32768,
    "reasoning": 32768,
}

# Safety margin for VRAM (GB)
_VRAM_SAFETY_MARGIN_GB = 2.0

# Context size buckets for ephemeral containers (powers of 2)
CONTEXT_SIZE_BUCKETS: List[int] = [8192, 16384, 32768, 65536, 131072]


@dataclass
class ExternalTokenBudget:
    """Runtime token budget from all sources injected into a conversation's context.

    Used to size ephemeral containers and ensure the context window is large
    enough to hold everything the LLM needs to see.

    Token estimation notes:
    - Hebrew/mixed-language content: ~3 chars per token (denser than English ~4)
    - Wiki pages: truncated to 4000 chars each ≈ 1333 tokens each
    - Pen & paper workspaces: JSON entries, estimate conservatively
    """

    pen_paper: int = 0        # tokens from pen_paper workspace reads
    wiki: int = 0             # tokens from wiki_query results
    history: int = 0          # rolling chat history tokens
    system: int = 0           # system prompt tokens (set once per conversation)
    reserve_response: int = 2048  # headroom reserved for model output

    @property
    def total(self) -> int:
        return self.pen_paper + self.wiki + self.history + self.system + self.reserve_response

    def __repr__(self) -> str:
        return (
            f"ExternalTokenBudget("
            f"pp={self.pen_paper}, wiki={self.wiki}, hist={self.history}, "
            f"sys={self.system}, reserve={self.reserve_response}, "
            f"total={self.total})"
        )


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for mixed Hebrew/English content.

    Uses len//3 rather than the typical len//4 for English-only text
    because Hebrew characters are multi-byte in UTF-8 and tokenize at
    a higher ratio (~2-3 chars per token vs ~4 for English).
    """
    if not text:
        return 0
    return max(1, len(text) // 3)


def bucket_context_size(required: int, buckets: Optional[List[int]] = None) -> int:
    """Return the smallest bucket that is >= required tokens."""
    bucket_list = sorted(buckets or CONTEXT_SIZE_BUCKETS)
    for b in bucket_list:
        if b >= required:
            return b
    return bucket_list[-1]


def recommend_context_for_budget(
    budget: ExternalTokenBudget,
    model_n_ctx_train: int = 131072,
    buckets: Optional[List[int]] = None,
) -> int:
    """Return the recommended (bucketed) context window for a given token budget.

    Args:
        budget: Accumulated token counts from all runtime sources.
        model_n_ctx_train: The model's hard training-context ceiling.
        buckets: Context size buckets to snap to. Defaults to CONTEXT_SIZE_BUCKETS.

    Returns:
        Recommended context window size, snapped up to the next bucket and
        clamped to [min_bucket, model_n_ctx_train].
    """
    required = budget.total
    bucketed = bucket_context_size(required, buckets)
    clamped = min(bucketed, model_n_ctx_train)
    log.debug(
        f"recommend_context_for_budget: required={required} "
        f"→ bucket={bucketed} → clamped={clamped} "
        f"(model_max={model_n_ctx_train})"
    )
    return clamped


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


def calculate_kv_cache_size(
    ctx_size: int,
    n_layer: int,
    n_embd: int,
    bytes_per_token: int = 2,
) -> float:
    """
    Calculate KV cache size in GB for a given context window.

    Formula: ctx_size * 2 * n_layer * n_embd * bytes_per_token
    The *2 is for K and V matrices.
    """
    if not n_layer or not n_embd:
        return 0.0

    total_bytes = ctx_size * 2 * n_layer * n_embd * bytes_per_token
    return total_bytes / (1024**3)


def estimate_vram_detailed(model_path: str, ctx_size: int) -> dict:
    """
    Calculate detailed VRAM breakdown for a model at a given context size.

    Returns dict with:
        weights_gb: VRAM for model weights (file size * 1.05)
        kv_cache_gb: VRAM for KV cache
        activation_gb: VRAM for activations (estimated)
        overhead_gb: Framework overhead (0.5 GB)
        total_gb: Total VRAM needed
    """
    metadata = read_gguf_metadata(model_path)
    file_size_gb = metadata.get("file_size_gb", 0)
    n_layer = metadata.get("n_layer")
    n_embd = metadata.get("n_embd")

    # Weights: file size * 1.05 (minimal overhead for loading)
    weights_gb = file_size_gb * 1.05

    # KV cache
    if n_layer and n_embd:
        kv_cache_gb = calculate_kv_cache_size(ctx_size, n_layer, n_embd)
    else:
        # Rough estimate: 0.5 GB per 8K context per 10GB model
        kv_cache_gb = (ctx_size / 8192) * (file_size_gb / 10) * 0.5

    # Activation memory (estimate based on model size and context)
    # Rough formula: 0.3 GB per 8K context per 10GB model
    activation_gb = (ctx_size / 8192) * (file_size_gb / 10) * 0.3

    # Framework overhead (llama.cpp runtime, CUDA context, etc.)
    overhead_gb = 0.5

    total_gb = weights_gb + kv_cache_gb + activation_gb + overhead_gb

    return {
        "weights_gb": round(weights_gb, 2),
        "kv_cache_gb": round(kv_cache_gb, 2),
        "activation_gb": round(activation_gb, 2),
        "overhead_gb": overhead_gb,
        "total_gb": round(total_gb, 2),
    }


def calculate_optimal_context(
    model_path: str,
    slot: str,
    available_vram_gb: float,
    other_slots_vram_gb: float = 0.0,
) -> dict:
    """
    Calculate optimal context window size for a model.

    Args:
        model_path: Path to the GGUF model file
        slot: Slot name (chat, utility, embedding, vision, reasoning)
        available_vram_gb: Total GPU VRAM available in GB
        other_slots_vram_gb: VRAM used by other running slots in GB

    Returns:
        dict with:
            recommended_ctx: int - recommended context size
            n_ctx_train: int - max context from model metadata
            vram_for_kv: float - VRAM needed for KV cache
            vram_for_weights: float - VRAM for model weights
            vram_for_activation: float - VRAM for activations
            vram_overhead: float - Framework overhead
            total_vram_needed: float - total VRAM needed
            reasoning: str - explanation of the calculation
    """
    metadata = read_gguf_metadata(model_path)
    file_size_gb = metadata.get("file_size_gb", 0)
    n_ctx_train = metadata.get("n_ctx_train")
    n_layer = metadata.get("n_layer")
    n_embd = metadata.get("n_embd")

    # Estimate VRAM for weights using detailed breakdown
    vram_detailed = estimate_vram_detailed(model_path, 8192)  # Use 8K as baseline
    vram_for_weights = vram_detailed["weights_gb"]
    vram_overhead = vram_detailed["overhead_gb"]

    # VRAM available for this slot (total - other slots - safety margin)
    vram_for_slot = available_vram_gb - other_slots_vram_gb - _VRAM_SAFETY_MARGIN_GB
    if vram_for_slot < vram_for_weights + vram_overhead:
        # Not enough VRAM even for weights - use minimum required context
        min_ctx = _MIN_CTX_SIZES.get(slot, 8192)
        return {
            "recommended_ctx": min_ctx,
            "n_ctx_train": n_ctx_train,
            "vram_for_kv": 0.0,
            "vram_for_weights": vram_for_weights,
            "vram_for_activation": 0.0,
            "vram_overhead": vram_overhead,
            "total_vram_needed": vram_for_weights + vram_overhead,
            "reasoning": f"Insufficient VRAM: {vram_for_slot:.1f}GB available, {vram_for_weights + vram_overhead:.1f}GB needed for weights+overhead. Using minimum context {min_ctx} for role '{slot}'.",
        }

    # VRAM available for KV cache and activation
    vram_for_kv_and_activation = vram_for_slot - vram_for_weights - vram_overhead

    # If we have n_layer and n_embd, calculate max context from VRAM
    if n_layer and n_embd and vram_for_kv_and_activation > 0:
        # Solve for ctx_size accounting for both KV cache and activation
        # KV cache: ctx_size * 2 * n_layer * n_embd * bytes_per_token
        # Activation: ctx_size * (file_size_gb / 10) * 0.3 / 8192 * 1024^3
        # Combined: ctx_size * (2 * n_layer * n_embd * 2 + activation_factor)
        
        bytes_per_token = 2  # FP16 KV cache
        activation_factor = (file_size_gb / 10) * 0.3 / 8192 * (1024**3)  # bytes per token
        
        max_ctx_from_vram = int(
            (vram_for_kv_and_activation * (1024**3)) / (2 * n_layer * n_embd * bytes_per_token + activation_factor)
        )

        # Round down to power of 2 multiple for efficiency
        def round_to_power_of_2(x):
            if x <= 0:
                return 2048
            power = 1
            while power * 2 <= x:
                power *= 2
            return power

        max_ctx_from_vram = round_to_power_of_2(max_ctx_from_vram)

        # Apply model's max context limit AND role minimum
        min_ctx = _MIN_CTX_SIZES.get(slot, 8192)
        if n_ctx_train:
            recommended_ctx = min(max_ctx_from_vram, n_ctx_train)
            # Ensure we meet the minimum for this role
            recommended_ctx = max(recommended_ctx, min_ctx)
            reasoning = (
                f"Model supports {n_ctx_train} tokens. "
                f"VRAM allows {max_ctx_from_vram} tokens. "
                f"Role '{slot}' requires minimum {min_ctx} tokens. "
                f"Using {recommended_ctx} tokens."
            )
        else:
            recommended_ctx = max(max_ctx_from_vram, min_ctx)
            reasoning = (
                f"Model max context unknown. "
                f"VRAM allows {max_ctx_from_vram} tokens. "
                f"Role '{slot}' requires minimum {min_ctx} tokens. "
                f"Using {recommended_ctx} tokens."
            )
    else:
        # Fallback: use minimum required context for this role
        min_ctx = _MIN_CTX_SIZES.get(slot, 8192)
        recommended_ctx = max(_DEFAULT_CTX_SIZES.get(slot, 8192), min_ctx)
        reasoning = (
            f"Could not calculate from metadata. "
            f"Role '{slot}' requires minimum {min_ctx} tokens. "
            f"Using {recommended_ctx} tokens."
        )

    # Calculate actual VRAM breakdown for recommended context
    actual_vram = estimate_vram_detailed(model_path, recommended_ctx)

    return {
        "recommended_ctx": recommended_ctx,
        "n_ctx_train": n_ctx_train,
        "vram_for_kv": actual_vram["kv_cache_gb"],
        "vram_for_weights": actual_vram["weights_gb"],
        "vram_for_activation": actual_vram["activation_gb"],
        "vram_overhead": actual_vram["overhead_gb"],
        "total_vram_needed": actual_vram["total_gb"],
        "reasoning": reasoning,
    }
