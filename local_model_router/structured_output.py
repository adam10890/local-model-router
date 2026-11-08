"""Check that a reply the client asked for as JSON actually is JSON.

A client asks with OpenAI's ``response_format``. The router forwards that
field untouched, because every target it talks to is an OpenAI-compatible
``/v1`` surface that understands it. What no one checks is the answer: a model
that ignores the field returns prose, the status is 200, and a tool-calling
harness receives a body it cannot load. ADR 0006 calls that the intolerable
daily failure, so this module exists to notice it.

Scope is deliberate. This validates **well-formedness**, not conformance to a
schema: `json.loads` succeeding is the line. A reply can parse here and still
violate the schema it was asked for. That is the known limit, chosen over a
new runtime dependency.

Pure functions only — no I/O, no network, no config. Results carry the parse
error and never the model's output, so they are safe in headers, telemetry and
error payloads alike.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

# Reason codes, in the convention the routing decision path already uses.
VALID = "structured_output_valid"
REPAIRED = "structured_output_repaired"
FAILED = "structured_output_failed"
UNSUPPORTED = "structured_output_unsupported"

ENFORCE_ENV = "A0_LMM_ROUTER_STRUCTURED_OUTPUT"

_JSON_FORMATS = frozenset({"json_object", "json_schema"})

# Models very often wrap valid JSON in a markdown fence. Rejecting that would
# be pedantry rather than safety, so it is unwrapped before parsing.
_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*\n(?P<body>.*?)\n?\s*```\s*$", re.DOTALL)


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of checking one reply. Never carries the reply itself."""

    ok: bool
    error: str = ""

    def describe(self) -> dict[str, Any]:
        return {"ok": self.ok, "error": self.error}


def json_output_requested(body: Any) -> bool:
    """Whether the request asked for JSON.

    Mirrors ``routing/catalog.py:_has_json_mode`` exactly: the same predicate
    decides which slots are eligible, so the two must not drift apart.
    """
    if not isinstance(body, dict):
        return False
    response_format = body.get("response_format")
    return isinstance(response_format, dict) and response_format.get("type") in _JSON_FORMATS


def extract_message_content(payload: Any) -> Optional[str]:
    """Return the assistant text from a chat completion, or None.

    None means there was nothing to check — a tool-call-only reply, an empty
    choices list, a malformed envelope. That is not a validation failure:
    this module only judges content it was actually given.
    """
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def strip_code_fence(text: str) -> str:
    match = _FENCE.match(text or "")
    return match.group("body") if match else (text or "")


def validate_reply(payload: Any) -> ValidationResult:
    """Check that the reply's content parses as JSON.

    A reply with no inspectable content passes: see extract_message_content.
    """
    content = extract_message_content(payload)
    if content is None:
        return ValidationResult(ok=True)

    candidate = strip_code_fence(content).strip()
    if not candidate:
        return ValidationResult(ok=False, error="the reply was empty")

    try:
        json.loads(candidate)
    except (ValueError, TypeError) as exc:
        # str(exc) is the parser's own message ("Expecting value: line 1
        # column 1"). It names a position, never the text at that position.
        return ValidationResult(ok=False, error=str(exc))
    return ValidationResult(ok=True)


def repair_instruction(result: ValidationResult) -> dict[str, str]:
    """The one message appended for the single retry.

    Carries the parser error and the requirement, never the failed output:
    echoing it back would put model text somewhere it is not meant to go, and
    the model already has it in context.
    """
    detail = result.error or "the reply was not valid JSON"
    return {
        "role": "system",
        "content": (
            "Your previous reply could not be parsed as JSON "
            f"({detail}). Reply again with valid JSON only: no prose, "
            "no explanation, and no markdown code fence."
        ),
    }


def repair_body(body: dict[str, Any], result: ValidationResult) -> dict[str, Any]:
    """A copy of the request with the repair instruction appended.

    The original body is left alone; callers reuse it for telemetry and
    headers after the retry.
    """
    retry = dict(body)
    messages = retry.get("messages")
    retry["messages"] = list(messages) + [repair_instruction(result)] if isinstance(
        messages, list
    ) else [repair_instruction(result)]
    return retry
