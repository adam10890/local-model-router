"""Passive bounded SSE usage extractor for local and upstream token accounting."""
from __future__ import annotations

import json
import re
from typing import Any, Dict

_MAX_LINE_BYTES = 65536
_EVENT_END = re.compile(rb"\r?\n\r?\n")


def _non_bool_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


class SseUsageExtractor:
    """Incrementally extract token counts from SSE chunks without keeping body text."""

    def __init__(self, *, max_line_bytes: int = _MAX_LINE_BYTES) -> None:
        self._max_line_bytes = max(256, int(max_line_bytes))
        self._buffer = bytearray()
        self._drop_until_newline = False
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._seen = False

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        view = memoryview(chunk)
        start = 0
        length = len(view)
        while start < length:
            nl = chunk.find(b"\n", start)
            if nl == -1:
                tail = view[start:]
                if self._drop_until_newline:
                    return
                if len(self._buffer) + len(tail) > self._max_line_bytes:
                    self._buffer.clear()
                    self._drop_until_newline = True
                    return
                self._buffer.extend(tail)
                return

            segment = view[start:nl]
            start = nl + 1
            if self._drop_until_newline:
                self._drop_until_newline = False
                self._buffer.clear()
                continue
            if len(self._buffer) + len(segment) > self._max_line_bytes:
                self._buffer.clear()
                continue
            if self._buffer:
                self._buffer.extend(segment)
                line = bytes(self._buffer)
                self._buffer.clear()
            else:
                line = bytes(segment)
            self._inspect_line(line)

    def finish(self) -> None:
        if self._buffer and not self._drop_until_newline:
            self._inspect_line(bytes(self._buffer))
        self._buffer.clear()
        self._drop_until_newline = False

    def _inspect_line(self, raw_line: bytes) -> None:
        stripped = raw_line.rstrip(b"\r").strip()
        if not stripped.startswith(b"data:"):
            return
        payload_bytes = stripped[5:].strip()
        if not payload_bytes or payload_bytes == b"[DONE]":
            return
        try:
            decoded = payload_bytes.decode("utf-8", errors="ignore")
            parsed = json.loads(decoded)
        except Exception:
            return
        if not isinstance(parsed, dict):
            return
        usage = parsed.get("usage")
        if not isinstance(usage, dict):
            return
        prompt_tokens = _non_bool_int(usage.get("prompt_tokens"))
        completion_tokens = _non_bool_int(usage.get("completion_tokens"))
        if prompt_tokens is None and completion_tokens is None:
            return
        if prompt_tokens is not None:
            self._prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            self._completion_tokens = completion_tokens
        self._seen = True

    def usage(self) -> Dict[str, int]:
        if not self._seen:
            return {}
        return {
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
        }


def request_stream_usage(payload: Dict[str, Any]) -> bool:
    """Ask a streaming upstream for its trailing usage chunk.

    Returns True when the router added the request itself, so the caller must
    hide that chunk from a client that never asked for it.
    """
    if payload.get("stream") is not True or "stream_options" in payload:
        return False
    payload["stream_options"] = {"include_usage": True}
    return True


def _is_usage_only_event(event: bytes) -> bool:
    for line in event.splitlines():
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        try:
            parsed = json.loads(line[5:].strip())
        except Exception:
            return False
        return isinstance(parsed, dict) and "usage" in parsed and not parsed.get("choices")
    return False


class UsageOnlyEventFilter:
    """Drop usage-only SSE events that the router requested on the client's behalf."""

    def __init__(self, *, max_buffer_bytes: int = _MAX_LINE_BYTES) -> None:
        self._max_buffer_bytes = max(256, int(max_buffer_bytes))
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> bytes:
        self._buffer.extend(chunk)
        out = bytearray()
        start = 0
        for match in _EVENT_END.finditer(self._buffer):
            event = bytes(self._buffer[start:match.end()])
            if not _is_usage_only_event(event):
                out.extend(event)
            start = match.end()
        del self._buffer[:start]
        if len(self._buffer) > self._max_buffer_bytes:
            # ponytail: an oversized event passes through unfiltered rather than buffering without bound.
            out.extend(self._buffer)
            self._buffer.clear()
        return bytes(out)

    def flush(self) -> bytes:
        rest = bytes(self._buffer)
        self._buffer.clear()
        return b"" if _is_usage_only_event(rest) else rest
