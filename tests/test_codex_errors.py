"""Codex failure messages must name the real problem.

`codex exec` writes a banner (workdir/model/session id) and echoes the whole
prompt to stderr *before* the actual error, and it logs a non-fatal
models-cache ERROR *first*. Truncating the head of that blob showed users a
cache-corruption message while hiding "you've hit your usage limit" — the one
line that explained the failure and what to do about it.
"""
from __future__ import annotations

from server.providers.codex_provider import _explain_failure, _is_terminal_failure

BANNER = (
    "2026-08-26T22:51:37.001704Z ERROR codex_models_manager::cache: failed to "
    "load models cache: missing field `supports_reasoning_summaries` at line 86\n"
    "OpenAI Codex v0.144.5\n--------\nworkdir: /tmp/x\nmodel: gpt-5.6-sol\n"
    "session id: 01a04045-992a-7a31-9986-2b20a0cdd29c\n--------\nuser\n"
    + ("You are the NEXT PROMPT COACH. " * 200)
    + "\n"
)
USAGE_LIMIT = (
    "ERROR: You've hit your usage limit. Upgrade to Pro "
    "(https://chatgpt.com/explore/pro), visit "
    "https://chatgpt.com/codex/settings/usage to purchase more credits or try "
    "again at 3:52 AM.\n"
)


def test_usage_limit_is_named_not_the_cache_warning():
    msg = _explain_failure(1, BANNER + USAGE_LIMIT + USAGE_LIMIT, "")
    assert "usage limit" in msg.lower()
    assert "3:52 AM" in msg
    # the non-fatal cache line must NOT be what the user is told about
    assert "supports_reasoning_summaries" not in msg
    assert "models cache" not in msg
    # nor should the echoed prompt leak into the toast
    assert "NEXT PROMPT COACH" not in msg
    # Reviews/Discuss are Codex-specific by design, so the shared message must
    # not tell people to "switch to Claude" — wrong advice on that path.
    assert "Settings" not in msg


def test_usage_limit_message_is_not_duplicated():
    """codex prints the error twice; the user should see it once."""
    msg = _explain_failure(1, BANNER + USAGE_LIMIT + USAGE_LIMIT, "")
    assert msg.lower().count("usage limit") == 1


def test_not_logged_in():
    msg = _explain_failure(1, "ERROR: Not logged in. Please authenticate.", "")
    assert "codex login" in msg


def test_api_detail_json_still_handled():
    msg = _explain_failure(1, '{"detail": "model not supported"}', "")
    assert "npm install -g @openai/codex" in msg


def test_unknown_failure_shows_tail_not_head():
    blob = BANNER + "the real failure is on the last line\n"
    msg = _explain_failure(1, blob, "")
    assert "the real failure is on the last line" in msg
    assert "NEXT PROMPT COACH" not in msg


def test_terminal_failures_should_not_be_retried_as_cold_start():
    assert _is_terminal_failure(_explain_failure(1, USAGE_LIMIT, "")) is True
    assert _is_terminal_failure("codex exec failed (exit 1): …odd") is False
