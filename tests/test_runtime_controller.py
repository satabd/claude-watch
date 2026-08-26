"""Tests for runtime controller pure logic: process identification, idle
heuristics, and the pending-prompt send idempotency guard."""
from __future__ import annotations

import json
import time

from server import db
from server.runtime.controller import (
    find_claude_processes,
    pane_title,
    parse_blocking_dialog,
    parse_status,
    project_name,
    session_is_busy,
    session_label,
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


# Captured verbatim from `zellij action dump-screen` on a real claude 2.1.220
# trust prompt. Two traps live in here: the question is *not* the nearest
# line above the options ("Security guide" is), and option 1's label wraps.
TRUST_SCREEN = """\
 Quick safety check: Is this a project you
 created or one you trust? (Like your own code,
 a well-known open source project, or work
 from your team). If not, take a moment to
 review what's in this folder first.

 Claude Code'll be able to read, edit, and
 execute files here.

 Security guide

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel
"""


def test_parse_trust_dialog():
    d = parse_blocking_dialog(TRUST_SCREEN)
    assert d is not None
    assert d["question"].endswith("first.")
    assert [o["n"] for o in d["options"]] == ["1", "2"]
    assert d["options"][0]["label"] == "Yes, I trust this folder"
    # the hint line must not be swallowed into the last option
    assert d["options"][1]["label"] == "No, exit"


def test_parse_dialog_inside_box_border():
    # Claude frames some dialogs; the border must not hide the options.
    screen = """\
╭──────────────────────────────────────────────╮
│ Do you want to make this edit to parser.py?  │
│                                              │
│ ❯ 1. Yes                                     │
│   2. Yes, allow all edits this session       │
│   3. No, and tell Claude what to do          │
│                                              │
│ Esc to cancel                                │
╰──────────────────────────────────────────────╯
"""
    d = parse_blocking_dialog(screen)
    assert d is not None
    assert d["question"] == "Do you want to make this edit to parser.py?"
    assert [o["label"] for o in d["options"]][0] == "Yes"
    assert len(d["options"]) == 3


def test_no_dialog_when_numbered_list_sits_above_composer_marker():
    # The composer's bare "❯" is on screen permanently — it must never make a
    # numbered list in ordinary output look like a selectable dialog.
    screen = """\
⏺ Steps:
  1. Install deps
  2. Run the server
────────────────────────────────
❯
────────────────────────────────
  ⏵⏵ auto mode on · ? for shortcuts
"""
    assert parse_blocking_dialog(screen) is None


# ---------------------------------------------------------------------------
# Naming: one zellij session per project, one pane per claude session
# ---------------------------------------------------------------------------

def test_project_name_is_the_folder_name():
    assert project_name("/Volumes/AI-STUDIO/Projects/rumailahub") == "rumailahub"
    assert project_name("/Users/sat/Dev/My App!") == "my-app"


def test_project_name_falls_back_to_bucket_then_constant():
    assert project_name(None, "-Users-sat-Dev-rumailahub") == "rumailahub"
    assert project_name(None, None) == "claude-watch"


def test_pane_title_prefers_ai_title_over_session_id():
    assert (
        pane_title(SID, "/x/rumailahub", "Fix the login flow")
        == "rumailahub-fix-the-login-flow"
    )
    assert pane_title(SID, "/x/rumailahub", None) == f"rumailahub-{SID[:8]}"


def test_session_label_ignores_a_title_that_slugs_to_nothing():
    assert session_label(SID, "!!!") == SID[:8]


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



# ---------------------------------------------------------------------------
# Ownership state machine (CLAUDE.md, "Ownership")
#
# managed  = a pid WE recorded, still alive, still a claude
# external = someone else's claude is alive on this transcript -> view-only
# inactive = nothing alive -> we may resume it
#
# `asyncio.run` rather than pytest-asyncio: these are the only async tests in
# the suite and it is not worth a dependency.
# ---------------------------------------------------------------------------

import asyncio

import pytest

from server.runtime import registry as _registry
from server.runtime.controller import ClaudeProc, ControlRefused, controller


@pytest.fixture
def world(tmp_path, monkeypatch, isolated_db):
    """An idle transcript, a Zellij that exists, and an empty process world."""
    import os

    p = tmp_path / f"{SID}.jsonl"
    _write_jsonl(p, [_user(), _assistant_text()])
    old = time.time() - 120
    os.utime(p, (old, old))
    monkeypatch.setattr(_registry, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(_registry, "claude_processes", lambda: {})
    monkeypatch.setattr(_registry, "unidentified_claudes", lambda cwd=None: [])
    monkeypatch.setattr(
        "server.runtime.controller.find_claude_processes", lambda lines=None: []
    )
    monkeypatch.setattr("server.runtime.zellij.zellij_bin", lambda: "/bin/zellij")
    return p


def _stage_owner(monkeypatch, pid, embedded=False):
    """Make `pid` look like a live claude driving SID, via the argv scan."""
    argv = f"claude --resume {SID}" + (" --print" if embedded else "")
    monkeypatch.setattr(
        "server.runtime.controller.find_claude_processes",
        lambda lines=None: [
            ClaudeProc(pid=pid, argv=argv, resume_id=SID, embedded=embedded)
        ],
    )
    monkeypatch.setattr(_registry, "claude_processes", lambda: {pid: argv})


def _state(path):
    return asyncio.run(controller.get_state(SID, path, cwd="/proj"))


def test_nothing_alive_is_resumable(world):
    s = _state(world)
    assert s.state == "inactive"
    assert s.controllable is True


def test_someone_elses_claude_is_view_only(world, monkeypatch):
    """The whole contract: an external session is never controllable while
    its process is alive, no matter how idle it looks."""
    _stage_owner(monkeypatch, 4242)
    s = _state(world)
    assert s.state in ("external_idle", "external_busy")
    assert s.controllable is False
    assert s.external_pid == 4242


def test_embedded_runtime_also_blocks_resume(world, monkeypatch):
    """A headless claude owns the transcript just as much as a TUI does."""
    _stage_owner(monkeypatch, 4242, embedded=True)
    assert _state(world).controllable is False


def test_ensure_managed_refuses_to_start_a_second_claude(world, monkeypatch):
    _stage_owner(monkeypatch, 4242)
    with pytest.raises(ControlRefused):
        asyncio.run(controller.ensure_managed(SID, world, "/proj"))


def test_unregistered_claude_in_project_blocks_resume(world, monkeypatch):
    """Cannot be named, so cannot be ruled out — refuse rather than risk a
    second writer on the transcript."""
    monkeypatch.setattr(
        _registry,
        "unidentified_claudes",
        lambda cwd=None: [_registry.UnidentifiedClaude(pid=777, cwd="/proj")],
    )
    s = _state(world)
    assert s.controllable is False
    assert "777" in (s.reason or "")


def test_binding_without_a_live_pid_is_retired(world):
    """A pane can outlive its claude. The binding describes a process, so if
    that process is gone the session is resumable — not managed."""
    db.runtime_binding_put(SID, "proj", "terminal_1", cwd="/proj", pid=999999)
    s = _state(world)
    assert s.state == "inactive"
    assert db.runtime_binding_get(SID) is None


def test_legacy_binding_adopts_the_single_live_owner(world, monkeypatch):
    """Rows written before pid was recorded are upgraded in place when the
    owner can be named exactly — no guessing from pane titles."""
    _stage_owner(monkeypatch, 4242)
    db.runtime_binding_put(SID, "proj", "terminal_1", cwd="/proj")  # pid NULL
    controller._resolve_binding_pid(
        SID, db.runtime_binding_get(SID), controller._live_pids_for(SID)
    )
    assert db.runtime_binding_get(SID)["pid"] == 4242


# ---------------------------------------------------------------------------
# New Claude Session — the front door. A session claude-watch starts is
# managed from birth, so it never needs ownership to be inferred later.
# ---------------------------------------------------------------------------

def test_new_session_is_managed_from_birth(world, monkeypatch, tmp_path):
    created: dict = {}

    async def fake_new_tab(name, tab, cwd, command):
        created["name"], created["tab"] = name, tab
        created["command"] = command
        return "terminal_7"

    async def noop(*a, **k):
        return None

    monkeypatch.setattr("server.runtime.zellij.create_session", noop)
    monkeypatch.setattr("server.runtime.zellij.new_tab", fake_new_tab)
    monkeypatch.setattr("server.runtime.zellij.rename_pane", noop)
    monkeypatch.setattr(
        controller, "_wait_tui_ready", lambda *a, **k: asyncio.sleep(0)
    )

    sid, state = asyncio.run(controller.create_session("/proj/rumailahub"))

    # A fresh id, started with --session-id so we own it from the first breath.
    assert created["command"][:3] == ["claude", "--session-id", sid]
    assert "--permission-mode" in created["command"]
    assert created["name"] == "rumailahub"
    assert created["tab"] == f"rumailahub-{sid[:8]}"
    assert state.state == "managed" and state.controllable is True
    # And the binding exists, which is what makes it managed rather than
    # something to be re-derived later.
    assert db.runtime_binding_get(sid)["zellij_session"] == "rumailahub"
    db.runtime_binding_delete(sid)


def test_new_session_ids_are_unique(world, monkeypatch):
    async def fake_new_tab(name, tab, cwd, command):
        return "terminal_7"

    async def noop(*a, **k):
        return None

    monkeypatch.setattr("server.runtime.zellij.create_session", noop)
    monkeypatch.setattr("server.runtime.zellij.new_tab", fake_new_tab)
    monkeypatch.setattr("server.runtime.zellij.rename_pane", noop)
    monkeypatch.setattr(
        controller, "_wait_tui_ready", lambda *a, **k: asyncio.sleep(0)
    )
    a, _ = asyncio.run(controller.create_session("/proj/x"))
    b, _ = asyncio.run(controller.create_session("/proj/x"))
    assert a != b
    db.runtime_binding_delete(a)
    db.runtime_binding_delete(b)


RESUME_DIALOG_UNDER_PROSE = """\
⏺ Both remaining workstreams are now merged. Summary:
  1. Per-customer rate limits (was: 2 failing tests). Root cause: WS-1 added only
  global buckets; per-customer keys were never sharded.
  3. Dependencies. Audit-driven, grouped, each group verified, lockfile regenerated
  web+admin (typechecks + 183 web tests green); posthog-js 1.420.0 → dompurify 3
──────────────────────────────────────────────────────────────
  This session is 6h 51m old and 213.7k tokens.
  Resuming the full session will consume a substantial portion of your usage limits. We recommend resuming from a summary.
  ❯ 1. Resume from summary (recommended)
    2. Resume full session as-is
    3. Don't ask me again
  Enter to confirm · Esc to cancel
"""


def test_dialog_found_below_prose_numbered_list():
    """Captured from a live resume: the assistant's own numbered list sits
    above the dialog. The scan used to collect the prose lines as "options",
    stop at the blank line, and report no dialog — while the user stared at
    one. The ❯ selection marker anchors the real block."""
    d = parse_blocking_dialog(RESUME_DIALOG_UNDER_PROSE)
    assert d is not None
    assert [o["n"] for o in d["options"]] == ["1", "2", "3"]
    assert d["options"][0]["label"].startswith("Resume from summary")
    assert "usage limits" in d["question"]
    # the prose list must NOT leak into the options
    assert all("rate limits" not in o["label"] for o in d["options"])


def test_dialog_detected_under_trailing_blank_rows():
    """The pane is 50 rows; a short dialog leaves ~40 blank rows under it.
    A fixed window over the raw grid saw only blanks."""
    short = (
        "  Do you want to proceed?\n"
        "  ❯ 1. Yes\n"
        "    2. No\n"
        "  Esc to cancel\n" + "\n" * 43
    )
    d = parse_blocking_dialog(short)
    assert d is not None
    assert [o["n"] for o in d["options"]] == ["1", "2"]
