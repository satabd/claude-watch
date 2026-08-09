"""Tests for runtime controller pure logic: process identification, idle
heuristics, and the pending-prompt send idempotency guard."""
from __future__ import annotations

import json
import time

from server import db
from server.runtime.controller import (
    find_claude_processes,
    parse_blocking_dialog,
    parse_status,
    session_is_busy,
    transcript_looks_terminal,
    zellij_session_name,
)

SID = "71214400-5a23-483a-858f-f9aae3f6df1c"


# ---------------------------------------------------------------------------
# Process discovery
# ---------------------------------------------------------------------------

def test_find_claude_tui_with_resume():
    lines = [
        f"  123 /opt/homebrew/bin/claude --resume {SID}",
        f"  124 /opt/homebrew/bin/claude --resume={SID} --model opus",
    ]
    procs = find_claude_processes(lines)
    assert len(procs) == 2
    assert all(p.resume_id == SID for p in procs)
    assert all(not p.embedded for p in procs)


def test_embedded_desktop_claude_is_flagged():
    lines = [
        "  17389 /Users/x/Library/Application Support/Claude/claude-code/2.1.222/"
        f"claude.app/Contents/MacOS/claude --output-format stream-json "
        f"--resume={SID} --permission-prompt-tool stdio"
    ]
    procs = find_claude_processes(lines)
    assert len(procs) == 1
    assert procs[0].embedded is True
    assert procs[0].resume_id == SID


def test_unrelated_processes_ignored():
    lines = [
        "  10 /usr/bin/grep claude",
        "  11 python -m uvicorn server.main:app",
        "  12 /bin/zsh -c claude-watch",
        f"  13 /opt/homebrew/bin/claude -p",  # headless one-shot (our providers)
    ]
    procs = find_claude_processes(lines)
    # only the bare `claude -p` matches the exe check; it must carry no resume
    # id and IS embedded-flagged via --print? -p alone → not embedded marker,
    # but resume_id is None so it can never be matched to a session.
    assert all(p.resume_id is None for p in procs)


def test_fresh_tui_without_resume_has_no_id():
    procs = find_claude_processes(["  55 /opt/homebrew/bin/claude"])
    assert len(procs) == 1
    assert procs[0].resume_id is None


def test_session_id_flag_also_identifies_owner():
    procs = find_claude_processes(
        [f"  60 /opt/homebrew/bin/claude --session-id {SID} --model opus"]
    )
    assert len(procs) == 1
    assert procs[0].resume_id == SID


# ---------------------------------------------------------------------------
# Idle detection
# ---------------------------------------------------------------------------

def _write_jsonl(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")


def _user(text="hi"):
    return {"type": "user", "message": {"content": text}}


def _assistant_text(text="done"):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _assistant_tool(tool_id="t1"):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": tool_id, "name": "Bash"}]},
    }


def _tool_result(tool_id="t1"):
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id}]},
    }


def test_terminal_shape_after_assistant_text(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_user(), _assistant_text()])
    assert transcript_looks_terminal(p) is True


def test_busy_shape_when_user_prompt_unanswered(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_assistant_text(), _user("new question")])
    assert transcript_looks_terminal(p) is False


def test_busy_shape_when_tool_use_pending(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_user(), _assistant_tool("t9")])
    assert transcript_looks_terminal(p) is False


def test_terminal_after_tool_result_and_text(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_user(), _assistant_tool(), _tool_result(), _assistant_text()])
    assert transcript_looks_terminal(p) is True


def test_turnless_transcript_is_terminal(tmp_path):
    """Metadata-only sessions (queue-operation/system/titles, zero turns)
    cannot be mid-turn — regression: they read as busy forever and became
    permanently uncontrollable."""
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {"type": "queue-operation", "op": "x"},
            {"type": "system", "content": "hook"},
            {"type": "ai-title", "aiTitle": "Make wakeel fully functional"},
        ],
    )
    assert transcript_looks_terminal(p) is True


def test_old_turnless_transcript_is_not_busy(tmp_path):
    import os

    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [{"type": "system", "content": "hook"}])
    old = time.time() - 3600
    os.utime(p, (old, old))
    assert session_is_busy(p) is False


def test_recent_mtime_means_busy(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_user(), _assistant_text()])
    # mtime is "now" -> busy regardless of terminal shape
    assert session_is_busy(p) is True


def test_old_mtime_and_terminal_shape_means_idle(tmp_path):
    import os

    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_user(), _assistant_text()])
    old = time.time() - 120
    os.utime(p, (old, old))
    assert session_is_busy(p) is False


# ---------------------------------------------------------------------------
# Blocking-dialog detection (permission prompts etc.)
# ---------------------------------------------------------------------------

PERMISSION_SCREEN = """\
 11 +    print(farewell("claude-watch"))
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to create ac_signal.py?
 ❯ 1. Yes
   2. Yes, allow all edits during this session
      (shift+tab)
   3. No
 Esc to cancel · Tab to amend
"""


def test_parse_permission_dialog():
    d = parse_blocking_dialog(PERMISSION_SCREEN)
    assert d is not None
    assert d["question"] == "Do you want to create ac_signal.py?"
    assert [o["n"] for o in d["options"]] == ["1", "2", "3"]
    # wrapped continuation line folded into option 2's label
    assert "shift+tab" in d["options"][1]["label"]
    assert d["options"][2]["label"] == "No"


def test_no_dialog_on_plain_composer():
    screen = """\
⏺ It prints Hello, claude-watch!
────────────────────────────────
❯
────────────────────────────────
  ⏸ manual mode on · ? for shortcuts
"""
    assert parse_blocking_dialog(screen) is None


def test_no_dialog_when_numbered_list_in_output():
    # Assistant output containing a numbered list must NOT read as a dialog
    # (no "Esc to cancel" marker on screen).
    screen = """\
⏺ Steps:
  1. Install deps
  2. Run the server
  3. Open the app
────────────────────────────────
❯
"""
    assert parse_blocking_dialog(screen) is None


# ---------------------------------------------------------------------------
# Status line: permission mode + live activity
# (fixtures captured verbatim from a real claude 2.x TUI pane)
# ---------------------------------------------------------------------------

def _screen(*tail: str) -> str:
    return "\n".join(["⏺ some earlier output", "─" * 48, "❯ ", "─" * 48, *tail])


def test_parse_mode_manual_idle():
    s = parse_status(_screen("  ⏸ manual mode on · ? for shortcuts · ← for a…"))
    assert s["mode"] == "manual"
    assert s["working"] is False
    assert s["activity"] is None


def test_parse_mode_accept_edits():
    s = parse_status(_screen("  ⏵⏵ accept edits on (shift+tab to cycle) · ← …"))
    assert s["mode"] == "accept_edits"


def test_parse_mode_plan():
    s = parse_status(_screen("  ⏸ plan mode on (shift+tab to cycle) · ← for …"))
    assert s["mode"] == "plan"


def test_parse_mode_bypass():
    s = parse_status(_screen("  ⏵⏵ bypass permissions on · ← for a…"))
    assert s["mode"] == "bypass"


def test_parse_mode_auto():
    # captured verbatim from `claude --permission-mode auto`
    s = parse_status(_screen("  ⏵⏵ auto mode on   ·"))
    assert s["mode"] == "auto"


def test_parse_mode_dont_ask():
    # captured verbatim from `claude --permission-mode dontAsk`
    s = parse_status(_screen("  ⏵⏵ don't ask on   ·"))
    assert s["mode"] == "dont_ask"


def test_parse_mode_truncated_by_narrow_pane():
    """A narrow pane truncates the status line — a real bypass pane renders
    just "⏵⏵ bypass". Losing the mode because the window is narrow would
    hide exactly the mode most worth showing."""
    s = parse_status(_screen("  ⏵⏵ bypass         ·"))
    assert s["mode"] == "bypass"


def test_plan_word_in_assistant_output_is_not_a_mode():
    """Mode detection is anchored to the ⏸/⏵⏵ status glyph, so ordinary
    output mentioning "plan" or "auto" can't be misread as a mode."""
    s = parse_status(
        _screen(
            "⏺ Here is the plan mode I would use for auto deployment",
            "  ⏸ manual mode on · ? for shortcuts · ← for a…",
        )
    )
    assert s["mode"] == "manual"


def test_working_detected_from_esc_to_interrupt():
    s = parse_status(_screen("  ⏸ manual mode on · esc to interrupt · ← for …"))
    assert s["working"] is True
    assert s["mode"] == "manual"


def test_working_detected_from_live_spinner():
    s = parse_status(
        _screen(
            "✽ Lollygagging… (5s · thought for 1s)",
            "  ⏸ manual mode on · esc to interrupt · ← for …",
        )
    )
    assert s["working"] is True
    assert s["activity"] == {
        "verb": "Lollygagging",
        "elapsed_s": 5,
        "detail": "thought for 1s",
    }


def test_finished_spinner_is_not_working():
    """"✻ Cooked for 2s" stays on screen as history after a turn ends — it
    must not read as an in-flight turn (no ellipsis/parens, no esc hint)."""
    s = parse_status(
        _screen("✻ Cooked for 2s", "  ⏸ manual mode on · ? for shortcuts · ← …")
    )
    assert s["working"] is False
    assert s["activity"] is None


def test_status_absent_on_unrecognized_screen():
    s = parse_status("just some text\nno status line here")
    assert s == {"mode": None, "working": False, "activity": None}


# ---------------------------------------------------------------------------
# Naming + pending prompt idempotency
# ---------------------------------------------------------------------------

def test_zellij_session_name_is_deterministic_prefix():
    assert zellij_session_name(SID) == "cw-71214400"


def test_pending_prompt_send_claim_is_single_shot(isolated_db):
    row = db.pending_prompt_add("bucket-a", SID, "review the diff")
    assert row["status"] == "pending"

    assert db.pending_prompt_claim(row["id"]) is True
    # A retry (double-click, UI retry, second tab) must NOT claim again.
    assert db.pending_prompt_claim(row["id"]) is False

    db.pending_prompt_finish(row["id"], ok=True)
    got = db.pending_prompt_get(row["id"])
    assert got["status"] == "sent"
    assert got["sent_ms"] is not None
    # sent prompts are neither editable nor deletable nor re-claimable
    assert db.pending_prompt_update_text(row["id"], "x") is False
    assert db.pending_prompt_delete(row["id"]) is False
    assert db.pending_prompt_claim(row["id"]) is False


def test_pending_prompt_failed_send_returns_to_pending(isolated_db):
    row = db.pending_prompt_add("bucket-a", SID, "hello")
    assert db.pending_prompt_claim(row["id"]) is True
    db.pending_prompt_finish(row["id"], ok=False)
    got = db.pending_prompt_get(row["id"])
    assert got["status"] == "pending"  # retryable
    assert db.pending_prompt_claim(row["id"]) is True


def test_pending_prompt_list_only_shows_pending(isolated_db):
    a = db.pending_prompt_add("b", SID, "one")
    db.pending_prompt_add("b", SID, "two")
    db.pending_prompt_claim(a["id"])
    db.pending_prompt_finish(a["id"], ok=True)
    listed = db.pending_prompt_list("b", SID)
    assert [r["text"] for r in listed] == ["two"]
