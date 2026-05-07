"""Tests for codex_provider session-id and tokens-used parsers.

These are the bits that feed Review Threads' provider continuity. We use
canned stderr blobs that mirror what a real ``codex exec`` run printed on
this machine during the planning step."""
from __future__ import annotations

from server.providers.codex_provider import (
    parse_session_id,
    parse_tokens_used,
)


REAL_STDERR = (
    "model: gpt-5.5\n"
    "provider: openai\n"
    "approval: never\n"
    "sandbox: read-only\n"
    "reasoning effort: low\n"
    "reasoning summaries: none\n"
    "session id: 019e04c3-47f2-76a1-9dc6-552b780511aa\n"
    "--------\n"
    "user\n"
    "say hi in 5 words\n"
    "\n"
    "codex\n"
    "Hi, nice to meet you.\n"
    "tokens used\n"
    "13,850\n"
)


def test_parse_session_id_pulls_uuid_from_real_stderr():
    sid = parse_session_id(REAL_STDERR)
    assert sid == "019e04c3-47f2-76a1-9dc6-552b780511aa"


def test_parse_session_id_returns_none_when_missing():
    assert parse_session_id("nothing here") is None


def test_parse_session_id_only_matches_uuid_shape():
    # Must not match a non-UUID after "session id:"
    assert parse_session_id("session id: not-a-uuid\n") is None


def test_parse_tokens_used_pulls_count_with_commas():
    assert parse_tokens_used(REAL_STDERR) == 13_850


def test_parse_tokens_used_returns_none_when_missing():
    assert parse_tokens_used("session id: aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n") is None


def test_parse_tokens_used_handles_no_commas():
    text = "tokens used\n42\n"
    assert parse_tokens_used(text) == 42
