"""Tests for server.parser.parse_line and is_visible."""
from __future__ import annotations

import json

from server.parser import is_visible, parse_line


def _line(obj: dict) -> str:
    return json.dumps(obj)


def test_assistant_line_with_text_thinking_and_tool_use():
    obj = {
        "type": "assistant",
        "uuid": "asst-1",
        "parentUuid": "u-0",
        "timestamp": "2025-01-01T00:00:00Z",
        "sessionId": "sess-1",
        "cwd": "/tmp/x",
        "gitBranch": "main",
        "entrypoint": "claude",
        "isSidechain": False,
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 1,
            },
            "content": [
                {"type": "text", "text": "Hello there"},
                {"type": "thinking", "thinking": "Let me think..."},
                {
                    "type": "tool_use",
                    "id": "tu-1",
                    "name": "Bash",
                    "input": {"command": "ls"},
                },
            ],
        },
    }
    ev = parse_line(_line(obj))
    assert ev is not None
    assert ev.type == "assistant"
    assert ev.role == "assistant"
    assert ev.uuid == "asst-1"
    assert ev.parent_uuid == "u-0"
    assert ev.session_id == "sess-1"
    assert ev.model == "claude-sonnet-4-6"
    assert ev.text_blocks == ["Hello there"]
    assert ev.thinking_blocks == ["Let me think..."]
    assert len(ev.tool_uses) == 1
    assert ev.tool_uses[0]["name"] == "Bash"
    assert ev.tool_uses[0]["input"] == {"command": "ls"}
    assert ev.usage is not None
    assert ev.usage["input_tokens"] == 10
    assert ev.usage["output_tokens"] == 20
    assert ev.usage["cache_read_input_tokens"] == 5
    assert ev.usage["cache_creation_input_tokens"] == 1


def test_user_line_with_string_content():
    obj = {
        "type": "user",
        "uuid": "u-1",
        "message": {"content": "hello world"},
    }
    ev = parse_line(_line(obj))
    assert ev is not None
    assert ev.role == "user"
    assert ev.user_text == "hello world"
    assert ev.is_command_artifact is False
    assert ev.tool_results == []


def test_user_line_with_tool_result_array_content():
    obj = {
        "type": "user",
        "uuid": "u-2",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu-1",
                    "content": "result body",
                    "is_error": False,
                },
                {"type": "text", "text": "extra text"},
            ]
        },
    }
    ev = parse_line(_line(obj))
    assert ev is not None
    assert ev.role == "user"
    assert ev.user_text is None
    assert len(ev.tool_results) == 1
    assert ev.tool_results[0]["tool_use_id"] == "tu-1"
    assert ev.tool_results[0]["content"] == "result body"
    assert ev.tool_results[0]["is_error"] is False
    assert ev.text_blocks == ["extra text"]


def test_ai_title_line():
    obj = {"type": "ai-title", "uuid": "t-1", "aiTitle": "My Cool Session"}
    ev = parse_line(_line(obj))
    assert ev is not None
    assert ev.type == "ai-title"
    assert ev.ai_title == "My Cool Session"


def test_pr_link_line():
    obj = {
        "type": "pr-link",
        "uuid": "pr-1",
        "prNumber": 42,
        "prUrl": "https://example.com/pr/42",
        "prRepository": "octo/repo",
    }
    ev = parse_line(_line(obj))
    assert ev is not None
    assert ev.type == "pr-link"
    assert ev.pr is not None
    assert ev.pr["number"] == 42
    assert ev.pr["url"] == "https://example.com/pr/42"
    assert ev.pr["repository"] == "octo/repo"


def test_empty_or_blank_line_returns_none():
    assert parse_line("") is None
    assert parse_line("   ") is None
    assert parse_line("\n") is None


def test_invalid_json_returns_none():
    assert parse_line("{not json") is None
    assert parse_line("garbage") is None


def test_is_visible_filters_command_artifacts():
    obj = {
        "type": "user",
        "uuid": "u-cmd",
        "message": {"content": "<command-name>/clear</command-name>"},
    }
    ev = parse_line(_line(obj))
    assert ev is not None
    assert ev.is_command_artifact is True
    assert is_visible(ev) is False


def test_is_visible_visible_user():
    obj = {"type": "user", "uuid": "u-v", "message": {"content": "hi"}}
    ev = parse_line(_line(obj))
    assert is_visible(ev) is True


def test_is_visible_hides_user_with_only_tool_results():
    obj = {
        "type": "user",
        "uuid": "u-tr",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu-1",
                    "content": "x",
                }
            ]
        },
    }
    ev = parse_line(_line(obj))
    assert is_visible(ev) is False


def test_is_visible_hides_assistant():
    """Pure-assistant events also aren't in the {user, assistant, pr-link} set?
    Actually they ARE in that set; verify a normal assistant event is visible."""
    obj = {
        "type": "assistant",
        "uuid": "a-1",
        "message": {"content": [{"type": "text", "text": "hi"}]},
    }
    ev = parse_line(_line(obj))
    assert is_visible(ev) is True


def test_is_visible_filters_system_type():
    obj = {"type": "system", "uuid": "sys-1"}
    ev = parse_line(_line(obj))
    assert ev is not None
    assert is_visible(ev) is False
