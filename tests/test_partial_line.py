"""Tests for server.watcher.JsonlTailer partial-line buffering and truncation."""
from __future__ import annotations

import json
from typing import Any

from server.watcher import JsonlTailer


class FakeBroadcaster:
    """Captures published payloads instead of fanning out to async subscribers."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


def _user_line(uuid: str, content: str = "hello") -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "message": {"content": content},
        }
    ) + "\n"


def test_partial_line_is_buffered_until_newline_arrives(tmp_path):
    bc = FakeBroadcaster()
    tailer = JsonlTailer(bc)  # type: ignore[arg-type]

    f = tmp_path / "session.jsonl"
    full = _user_line("u-1", "complete")
    half = _user_line("u-2", "second")
    # half-line: drop the final newline + last few chars
    half_truncated = half[:-5]  # missing the last 5 chars (incl. the trailing "}\n")

    f.write_bytes((full + half_truncated).encode("utf-8"))

    tailer.consume_new(f)

    # Only the complete line was emitted as an `event`
    event_payloads = [e for e in bc.events if e["kind"] == "event"]
    assert len(event_payloads) == 1
    assert event_payloads[0]["event"]["uuid"] == "u-1"
    # The offset should have rewound past the half-line
    expected_offset = len(full.encode("utf-8"))
    assert tailer.offsets[str(f)] == expected_offset

    # Now append the rest of the half-line (the missing 5 chars)
    bc.events.clear()
    f.write_bytes((full + half).encode("utf-8"))

    tailer.consume_new(f)
    event_payloads = [e for e in bc.events if e["kind"] == "event"]
    assert len(event_payloads) == 1
    assert event_payloads[0]["event"]["uuid"] == "u-2"
    # Offset should now be at end of file
    assert tailer.offsets[str(f)] == len((full + half).encode("utf-8"))


def test_truncation_resets_offset_to_zero(tmp_path):
    bc = FakeBroadcaster()
    tailer = JsonlTailer(bc)  # type: ignore[arg-type]

    f = tmp_path / "session.jsonl"
    line_a = _user_line("u-A", "first")
    line_b = _user_line("u-B", "second")
    f.write_bytes((line_a + line_b).encode("utf-8"))

    tailer.consume_new(f)
    # Both lines emitted
    event_payloads = [e for e in bc.events if e["kind"] == "event"]
    assert {e["event"]["uuid"] for e in event_payloads} == {"u-A", "u-B"}
    full_size = len((line_a + line_b).encode("utf-8"))
    assert tailer.offsets[str(f)] == full_size

    # Simulate truncation: shrink the file to a single fresh line
    bc.events.clear()
    line_c = _user_line("u-C", "after-trunc")
    f.write_bytes(line_c.encode("utf-8"))
    # File is now smaller than the previous offset
    assert f.stat().st_size < full_size

    tailer.consume_new(f)
    event_payloads = [e for e in bc.events if e["kind"] == "event"]
    # The reset means the entire (new) file is treated as fresh
    assert len(event_payloads) == 1
    assert event_payloads[0]["event"]["uuid"] == "u-C"
    assert tailer.offsets[str(f)] == len(line_c.encode("utf-8"))
