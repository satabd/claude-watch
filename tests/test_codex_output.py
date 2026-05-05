"""Tests for codex_provider output parsing and prompt_writer split."""
from __future__ import annotations

from server.providers.codex_provider import (
    _clean_stdout_only,
    _extract_from_structured,
)
from server.routes.prompt_writer import _split_output

# The user spec mentioned `_extract_final_message`; the actual implementation
# function is `_extract_from_structured` (it's the same role: "find the final
# answer body in the structured codex output"). We alias it so the tests stay
# readable and so future renames are localized.
_extract_final_message = _extract_from_structured


def test_extract_from_structured_stderr_with_dashes_and_tokens_used():
    text = (
        "--------\n"
        "model: gpt-5\n"
        "--------\n"
        "thinking\nplanning the answer\n\n"
        "codex\n"
        "Hello, this is the answer.\n"
        "tokens used: 123\n"
    )
    out = _extract_final_message(text)
    assert "Hello, this is the answer." in out
    assert "tokens used" not in out
    assert "thinking" not in out


def test_extract_from_structured_last_codex_wins():
    text = (
        "codex\nfirst (stale) answer line\n"
        "tokens used: 1\n"
        "codex\nFINAL answer\n"
        "tokens used: 2\n"
    )
    out = _extract_final_message(text)
    assert out == "FINAL answer"
    assert "first (stale)" not in out


def test_extract_from_structured_no_codex_marker_returns_empty():
    out = _extract_final_message("some random text\nwith no marker\n")
    assert out == ""


def test_extract_from_structured_strips_success_lines():
    text = (
        "codex\n"
        "Real answer line\n"
        "SUCCESS: The process with PID 1234 has been terminated.\n"
        "execution error: blah\n"
        "More answer text\n"
        "tokens used: 5\n"
    )
    out = _extract_final_message(text)
    assert "Real answer line" in out
    assert "More answer text" in out
    assert "SUCCESS" not in out
    assert "execution error" not in out


def test_clean_stdout_only_strips_sandbox_noise():
    text = (
        "Plain answer text\n"
        "SUCCESS: The process with PID 999 has been terminated.\n"
        "Second line of answer\n"
        "execution error something\n"
    )
    out = _clean_stdout_only(text)
    assert "Plain answer text" in out
    assert "Second line of answer" in out
    assert "SUCCESS" not in out
    assert "execution error" not in out


def test_clean_stdout_only_no_noise():
    out = _clean_stdout_only("just\ntwo lines")
    assert out == "just\ntwo lines"


def test_split_output_extracts_prompt_block_and_notes():
    raw = (
        "What I improved: tightened the language.\n\n"
        "```prompt\n"
        "Refactor this function to be pure.\n"
        "```\n"
        "Assumptions:\n- input is small\n"
    )
    prompt, notes = _split_output(raw)
    assert prompt == "Refactor this function to be pure."
    assert "What I improved" in notes
    assert "Assumptions" in notes


def test_split_output_with_unlabeled_fence():
    raw = "```\nthe polished prompt\n```\nleftover notes"
    prompt, notes = _split_output(raw)
    assert prompt == "the polished prompt"
    assert "leftover notes" in notes


def test_split_output_falls_back_when_no_fence():
    raw = "plain output with no fenced block"
    prompt, notes = _split_output(raw)
    assert prompt == "plain output with no fenced block"
    assert notes == ""
