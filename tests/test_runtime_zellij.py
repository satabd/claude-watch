"""Tests for the Zellij CLI wrapper's pure logic (parsing + paste encoding)."""
from __future__ import annotations

from server.runtime.zellij import parse_sessions, paste_chunks


def test_parse_sessions_alive_and_exited():
    raw = (
        "jumping-peach [Created 45m 31s ago] \n"
        "cw-71214400 [Created 1m 55s ago] (EXITED - attach to resurrect)\n"
        "\n"
    )
    got = parse_sessions(raw)
    assert [(s.name, s.exited) for s in got] == [
        ("jumping-peach", False),
        ("cw-71214400", True),
    ]


def test_parse_sessions_empty():
    assert parse_sessions("") == []


def test_paste_chunks_single_line_is_plain_chars():
    steps = paste_chunks('fix the "auth" bug — عربي 汉字 $(no shell)')
    assert steps == [("chars", 'fix the "auth" bug — عربي 汉字 $(no shell)')]


def test_paste_chunks_multiline_wraps_in_bracketed_paste():
    steps = paste_chunks("line one\nline two")
    assert steps[0] == ("bytes", [27, 91, 50, 48, 48, 126])  # ESC[200~
    assert steps[1] == ("chars", "line one\nline two")
    assert steps[2] == ("bytes", [27, 91, 50, 48, 49, 126])  # ESC[201~


def test_paste_chunks_normalizes_crlf():
    steps = paste_chunks("a\r\nb\rc")
    assert steps[1] == ("chars", "a\nb\nc")
