"""
Failover support for LMM Router slot chains.

Adapted from tiny_router/tiny_router_helpers/failover.py.

Key design rules:
- Works with slot URLs (e.g., "http://host.docker.internal:8080") instead of preset names
- Supports configurable failover chains per slot role
- Tracks failover state for recovery
- Emits events via A0 notifications (optional)
"""

import re
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set
from enum import Enum

logger = logging.getLogger("lmm_router.failover")


# ---------------------------------------------------------------------------
# HTTP status codes that should trigger a failover attempt.
# ---------------------------------------------------------------------------
FAILOVER_HTTP_STATUSES: Set[int] = frozenset({408, 429, 500, 502, 503, 504})


# ---------------------------------------------------------------------------
# Error text markers
# ---------------------------------------------------------------------------
_TIMEOUT_MARKERS = (
    "timeout",
    "timed out",
    "deadline exceeded",
    "context deadline exceeded",
    "read timed out",
    "connection timed out",
)
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "too many requests",
    "429",
    "weekly usage limit",
)
_QUOTA_MARKERS = (
    "insufficient_quota",
    "quota exceeded",
    "out of credits",
    "out of tokens",
    "resource exhausted",
)
_PROVIDER_MARKERS = (
    "service unavailable",
    "temporarily unavailable",
    "connection error",
    "connection reset",
    "provider error",
    "apiconnectionerror",
    "llama.cpp server unavailable",
    "failed to connect",
    "connection refused",
)


class FailoverReason(str, Enum):
    """Classification of failover reasons."""
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    QUOTA_EXHAUSTED = "quota_exhausted"
    PROVIDER_ERROR = "provider_error"
    HTTP_ERROR = "http_error"
    SLOT_UNHEALTHY = "slot_unhealthy"
    UNKNOWN_ERROR = "unknown_error"


# ---------------------------------------------------------------------------
# Error classification helpers
# ---------------------------------------------------------------------------

def _extract_error_text(exc: Exception) -> str:
    """Return a single string from the exception's args, falling back to str()."""
    return (
        " ".join(str(part) for part in getattr(exc, "args", ()) if part).strip()
        or str(exc)
    )


def _extract_status_code(exc: Exception, text: str) -> Optional[int]:
    """Return an HTTP status code from the exception attribute or embedded JSON."""
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    match = re.search(r"statuscode\"\s*:\s*(\d+)", text, re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def classify_failover_reason(exc: Exception) -> Dict[str, Any]:
    """
    Classify the failure reason for a given exception.

    Returns a dict with keys:
        reason      – FailoverReason label
        status_code – int or None
        error_text  – raw text extracted from the exception
    """
    raw_text = _extract_error_text(exc)
    text = raw_text.lower()
    status_code = _extract_status_code(exc, raw_text)

    if isinstance(status_code, int):
        if status_code == 429:
            reason = FailoverReason.RATE_LIMIT
        elif status_code in (408, 504):
            reason = FailoverReason.TIMEOUT
        elif status_code >= 500:
            reason = FailoverReason.PROVIDER_ERROR
        else:
            reason = FailoverReason.HTTP_ERROR
        return {"reason": reason, "status_code": status_code, "error_text": raw_text}

    if any(marker in text for marker in _TIMEOUT_MARKERS):
        return {"reason": FailoverReason.TIMEOUT, "status_code": None, "error_text": raw_text}
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return {"reason": FailoverReason.RATE_LIMIT, "status_code": None, "error_text": raw_text}
    if any(marker in text for marker in _QUOTA_MARKERS):
        return {"reason": FailoverReason.QUOTA_EXHAUSTED, "status_code": None, "error_text": raw_text}
    if any(marker in text for marker in _PROVIDER_MARKERS):
        return {"reason": FailoverReason.PROVIDER_ERROR, "status_code": None, "error_text": raw_text}

    return {"reason": FailoverReason.UNKNOWN_ERROR, "status_code": None, "error_text": raw_text}


_FAILOVER_REASONS: Set[FailoverReason] = frozenset({
    FailoverReason.TIMEOUT,
    FailoverReason.RATE_LIMIT,
    FailoverReason.QUOTA_EXHAUSTED,
    FailoverReason.PROVIDER_ERROR,
    FailoverReason.SLOT_UNHEALTHY,
})


def should_failover(exc: Exception) -> bool:
    """
    Return True when the exception represents a transient LLM/provider failure
    that warrants trying the next slot in the failover chain.
    """
    raw_text = _extract_error_text(exc)
    status_code = _extract_status_code(exc, raw_text)

    if isinstance(status_code, int):
        if status_code in FAILOVER_HTTP_STATUSES or status_code >= 500:
            return True
        return False

    reason_info = classify_failover_reason(exc)
    return reason_info["reason"] in _FAILOVER_REASONS


# ---------------------------------------------------------------------------
# Slot chain navigation
# ---------------------------------------------------------------------------

@dataclass
class SlotFailoverState:
    """Tracks the current position in a slot failover chain."""

    current_slot: str = ""                    # Current slot ID
    chain: List[str] = field(default_factory=list)  # List of slot IDs in order
    chain_index: int = -1
    failed_at: float = 0.0
    recovery_seconds: int = 300
    last_failover_reason: str = ""
    failover_count: int = 0


def get_next_in_chain(current: str, chain: List[str]) -> Optional[str]:
    """
    Return the next slot in *chain* that comes after *current*.

    If *current* is not in the chain, the first element is returned.
    Returns None when the chain is exhausted.
    """
    if not chain:
        return None
    try:
        idx = chain.index(current)
    except ValueError:
        # current not found — start from chain[0]
        return chain[0]
    next_idx = idx + 1
    if next_idx >= len(chain):
        return None
    return chain[next_idx]


def should_recover(state: SlotFailoverState) -> bool:
    """
    Return True if the recovery interval has elapsed since the failure was
    recorded, indicating it is safe to try the original slot again.
    """
    if state.failed_at == 0.0:
        return False
    elapsed = time.monotonic() - state.failed_at
    return elapsed >= state.recovery_seconds


def reset_state(state: SlotFailoverState) -> None:
    """Reset *state* to its pre-failure defaults."""
    state.current_slot = state.chain[0] if state.chain else ""
    state.chain_index = 0 if state.chain else -1
    state.failed_at = 0.0
    state.last_failover_reason = ""


def record_failover(state: SlotFailoverState, reason: str) -> None:
    """Record a failover event in the state."""
    state.failed_at = time.monotonic()
    state.last_failover_reason = reason
    state.failover_count += 1
    next_slot = get_next_in_chain(state.current_slot, state.chain)
    if next_slot:
        state.current_slot = next_slot
        try:
            state.chain_index = state.chain.index(next_slot)
        except ValueError:
            state.chain_index = -1


# ---------------------------------------------------------------------------
# Default chains by role
# ---------------------------------------------------------------------------

DEFAULT_CHAINS: Dict[str, List[str]] = {
    # Role -> ordered list of slot IDs to try
    "chat": ["chat", "utility", "openrouter_fallback"],
    "utility": ["utility", "chat", "openrouter_fallback"],
    "embed": ["embed"],  # No fallback for embeddings
}


def get_chain_for_role(role: str, custom_chains: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """Get the failover chain for a given role."""
    chains = custom_chains or {}
    return chains.get(role, DEFAULT_CHAINS.get(role, [role]))


# ---------------------------------------------------------------------------
# Slot decision result
# ---------------------------------------------------------------------------

@dataclass
class SlotDecision:
    """Result of a slot selection decision with failover info."""

    slot_id: str                        # Selected slot ID
    url: str                           # Full URL (e.g., "http://host.docker.internal:8080")
    role: str                          # Role (chat, utility, embed)
    reason: str                        # Human-readable selection reason
    fallback_chain: List[str] = field(default_factory=list)
    is_failover: bool = False          # True if this is a failover selection
    failover_reason: str = ""          # Reason for failover (if is_failover)
    classification: Dict[str, Any] = field(default_factory=dict)


def create_decision(
    slot_id: str,
    url: str,
    role: str,
    reason: str,
    chain: Optional[List[str]] = None,
    classification: Optional[Dict[str, Any]] = None,
) -> SlotDecision:
    """Create a SlotDecision with proper defaults."""
    return SlotDecision(
        slot_id=slot_id,
        url=url,
        role=role,
        reason=reason,
        fallback_chain=chain or [slot_id],
        is_failover=False,
        classification=classification or {},
    )


# ---------------------------------------------------------------------------
# Cooldown probe support
# ---------------------------------------------------------------------------

@dataclass
class CooldownProbe:
    """Configuration for cooldown probing of ERROR slots."""

    enabled: bool = True
    interval_seconds: int = 30
    max_attempts: int = 10
    probe_timeout: int = 5


class CooldownTracker:
    """Tracks cooldown state for slots in ERROR status."""

    def __init__(self):
        self._error_slots: Dict[str, Dict[str, Any]] = {}
        self._probe_task: Any = None

    def mark_error(self, slot_id: str, error_message: str = "") -> None:
        """Mark a slot as in ERROR state and start tracking it."""
        if slot_id not in self._error_slots:
            self._error_slots[slot_id] = {
                "error_at": time.monotonic(),
                "error_message": error_message,
                "probe_count": 0,
                "recovered": False,
            }
        logger.info(f"Slot '{slot_id}' marked for cooldown probing")

    def mark_recovered(self, slot_id: str) -> None:
        """Mark a slot as recovered."""
        if slot_id in self._error_slots:
            self._error_slots[slot_id]["recovered"] = True
            del self._error_slots[slot_id]
            logger.info(f"Slot '{slot_id}' recovered and removed from cooldown tracking")

    def should_probe(self, slot_id: str, probe_config: CooldownProbe) -> bool:
        """Check if a slot should be probed for recovery."""
        if slot_id not in self._error_slots:
            return False
        info = self._error_slots[slot_id]
        if info["recovered"]:
            return False
        if info["probe_count"] >= probe_config.max_attempts:
            return False
        elapsed = time.monotonic() - info["error_at"]
        # Probe every interval_seconds
        return elapsed >= (info["probe_count"] * probe_config.interval_seconds)

    def record_probe(self, slot_id: str) -> None:
        """Record that a probe was attempted."""
        if slot_id in self._error_slots:
            self._error_slots[slot_id]["probe_count"] += 1

    def get_error_slots(self) -> List[str]:
        """Get list of slot IDs currently being tracked."""
        return list(self._error_slots.keys())

    def get_status(self, slot_id: str) -> Optional[Dict[str, Any]]:
        """Get cooldown status for a slot."""
        return self._error_slots.get(slot_id)


# Global cooldown tracker instance
_cooldown_tracker: Optional[CooldownTracker] = None


def get_cooldown_tracker() -> CooldownTracker:
    """Get the global cooldown tracker instance."""
    global _cooldown_tracker
    if _cooldown_tracker is None:
        _cooldown_tracker = CooldownTracker()
    return _cooldown_tracker
