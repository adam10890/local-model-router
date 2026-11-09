"""Unit tests for the structured-output checker.

Pure functions, no HTTP. The gate that wires these into the forward path is
covered in tests/test_structured_output_gate.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_model_router import structured_output as so  # noqa: E402

_PROSE = "Sure! Here is what you asked for."


def _reply(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


# ---------------------------------------------------------------------------
# Did the client ask for JSON
# ---------------------------------------------------------------------------

class TestJsonOutputRequested:
    @pytest.mark.parametrize("kind", ["json_object", "json_schema"])
    def test_recognized_formats(self, kind):
        assert so.json_output_requested({"response_format": {"type": kind}}) is True

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"response_format": {"type": "text"}},
            {"response_format": "json_object"},
            {"response_format": None},
            None,
            "not a dict",
        ],
    )
    def test_everything_else_is_not_a_json_request(self, body):
        assert so.json_output_requested(body) is False

    def test_matches_the_routing_predicate(self):
        """The router picks slots with routing/catalog.py's predicate.

        If these two ever disagree, a request can be routed to a JSON-capable
        slot and then not checked, or checked after being routed anywhere.
        """
        from local_model_router.routing.catalog import _has_json_mode

        for kind in ("json_object", "json_schema", "text", "nonsense"):
            body = {"response_format": {"type": kind}}
            assert so.json_output_requested(body) == _has_json_mode(body)


# ---------------------------------------------------------------------------
# Pulling the content out
# ---------------------------------------------------------------------------

class TestExtractMessageContent:
    def test_reads_the_first_choice(self):
        assert so.extract_message_content(_reply("hi")) == "hi"

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            "not a dict",
            {},
            {"choices": []},
            {"choices": "no"},
            {"choices": [None]},
            {"choices": [{}]},
            {"choices": [{"message": "no"}]},
            {"choices": [{"message": {"content": None}}]},
            {"choices": [{"message": {"tool_calls": [{"id": "1"}]}}]},
        ],
    )
    def test_nothing_to_read_is_none(self, payload):
        assert so.extract_message_content(payload) is None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidateReply:
    @pytest.mark.parametrize(
        "content",
        [
            '{"a": 1}',
            "[1, 2, 3]",
            '  {"a": 1}  ',
            '```json\n{"a": 1}\n```',
            "```\n{}\n```",
            '"a bare string is valid JSON"',
        ],
    )
    def test_valid_json_passes(self, content):
        assert so.validate_reply(_reply(content)).ok is True

    @pytest.mark.parametrize("content", [_PROSE, "", "   ", "{not json", "{'single': 'quotes'}"])
    def test_invalid_json_fails(self, content):
        assert so.validate_reply(_reply(content)).ok is False

    def test_a_reply_with_nothing_to_check_passes(self):
        """A tool-call-only reply is not a JSON failure; there is no content."""
        assert so.validate_reply({"choices": [{"message": {"tool_calls": [{}]}}]}).ok is True

    def test_failure_carries_a_parser_error(self):
        result = so.validate_reply(_reply(_PROSE))
        assert result.ok is False
        assert result.error

    def test_failure_never_carries_the_model_output(self):
        """The result travels into headers, telemetry and error bodies."""
        result = so.validate_reply(_reply(_PROSE))
        blob = json.dumps(result.describe())
        assert "Sure" not in blob
        assert "asked for" not in blob

    def test_an_empty_reply_is_reported_distinctly(self):
        assert "empty" in so.validate_reply(_reply("   ")).error


class TestStripCodeFence:
    @pytest.mark.parametrize(
        "fenced,expected",
        [
            ('```json\n{"a": 1}\n```', '{"a": 1}'),
            ('```JSON\n{"a": 1}\n```', '{"a": 1}'),
            ('```\n{"a": 1}\n```', '{"a": 1}'),
            ('{"a": 1}', '{"a": 1}'),
            ("", ""),
        ],
    )
    def test_unwraps_a_fence_and_leaves_everything_else(self, fenced, expected):
        assert so.strip_code_fence(fenced) == expected


# ---------------------------------------------------------------------------
# The repair message
# ---------------------------------------------------------------------------

class TestRepair:
    def test_instruction_names_the_parser_error(self):
        result = so.validate_reply(_reply(_PROSE))
        message = so.repair_instruction(result)
        assert message["role"] == "system"
        assert result.error in message["content"]

    def test_instruction_never_echoes_the_failed_output(self):
        message = so.repair_instruction(so.validate_reply(_reply(_PROSE)))
        assert "Sure" not in message["content"]

    def test_repair_body_appends_one_message(self):
        body = {"messages": [{"role": "user", "content": "hi"}], "model": "m"}
        repaired = so.repair_body(body, so.validate_reply(_reply(_PROSE)))
        assert len(repaired["messages"]) == 2
        assert repaired["messages"][-1]["role"] == "system"
        assert repaired["model"] == "m"

    def test_repair_body_does_not_mutate_the_original(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        so.repair_body(body, so.validate_reply(_reply(_PROSE)))
        assert len(body["messages"]) == 1

    def test_repair_body_copes_with_no_messages(self):
        repaired = so.repair_body({"model": "m"}, so.validate_reply(_reply(_PROSE)))
        assert len(repaired["messages"]) == 1


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------

def test_reason_codes_are_distinct_and_prefixed():
    codes = {so.VALID, so.REPAIRED, so.FAILED, so.UNSUPPORTED}
    assert len(codes) == 4
    assert all(code.startswith("structured_output_") for code in codes)
