"""Tests for Claude Code's live-session registry reader.

The registry is what stops claude-watch starting a second `claude --resume`
on a transcript another claude is already writing. Fixtures are shaped after
real `~/.claude/sessions/<pid>.json` files.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from server.runtime import registry

SID = "4db641a9-73a5-4139-b9d4-d92a7e87465b"
OTHER = "7d5d53ee-ec19-483a-8754-a1f424c48805"


def _write(tmp_path, pid, **over):
    rec = {
        "pid": pid,
        "sessionId": SID,
        "cwd": "/Volumes/AI-STUDIO/Projects/claude-watch",
        "startedAt": 1786321252492,
        "version": "2.1.220",
        "kind": "interactive",
        "entrypoint": "cli",
        "status": "idle",
        "updatedAt": int(time.time() * 1000),
    }
    rec.update(over)
    (tmp_path / f"{pid}.json").write_text(json.dumps(rec))
    return rec


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setattr(registry, "SESSIONS_DIR", d)
    # A record is only believed when its pid is still a *claude*. The test
    # process is not one, so stand in for `ps` and vouch for it — otherwise
    # every fixture record would be retired as a recycled pid.
    monkeypatch.setattr(
        registry, "claude_processes", lambda: {os.getpid(): "claude"}
    )
    return d


# A pid that is certainly alive (us) and one that is certainly not.
ALIVE = os.getpid()
DEAD = 2**22  # above any real pid on macOS/Linux defaults


def test_identifies_owner_without_any_resume_flag(sessions_dir):
    """The whole point: a plain `claude` carries no --resume in its argv, so
    only the registry can tie it to a session id."""
    _write(sessions_dir, ALIVE)
    owners = registry.owners_of(SID)
    assert [o.pid for o in owners] == [ALIVE]
    assert owners[0].takeoverable is True
    assert owners[0].busy is False


def test_dead_pids_are_ignored(sessions_dir):
    """Records outlive crashes; a stale file must not block control."""
    _write(sessions_dir, DEAD)
    assert registry.owners_of(SID) == []


def test_other_sessions_are_not_owners(sessions_dir):
    _write(sessions_dir, ALIVE, sessionId=OTHER)
    assert registry.owners_of(SID) == []


def test_busy_status_is_read(sessions_dir):
    _write(sessions_dir, ALIVE, status="busy")
    assert registry.owners_of(SID)[0].busy is True


def test_stale_heartbeat_makes_status_unknown(sessions_dir):
    """An old heartbeat must read as unknown, not as a confident 'idle' —
    the caller treats unknown as busy rather than risk a double-spawn."""
    old = int((time.time() - registry.STATUS_FRESH_S - 60) * 1000)
    _write(sessions_dir, ALIVE, status="idle", updatedAt=old)
    assert registry.owners_of(SID)[0].busy is None


def test_desktop_sessions_are_not_takeoverable(sessions_dir):
    """Claude Desktop has no TTY to free; killing it would sever a
    conversation happening in another app."""
    _write(sessions_dir, ALIVE, entrypoint="claude-desktop")
    assert registry.owners_of(SID)[0].takeoverable is False


def test_filename_pid_must_match_record(sessions_dir):
    (sessions_dir / "999999.json").write_text(
        json.dumps({"pid": ALIVE, "sessionId": SID})
    )
    assert registry.owners_of(SID) == []


def test_malformed_and_empty_files_are_skipped(sessions_dir):
    (sessions_dir / "1.json").write_text("not json")
    (sessions_dir / "2.json").write_text("{}")
    _write(sessions_dir, ALIVE)
    assert [o.pid for o in registry.owners_of(SID)] == [ALIVE]


def test_missing_directory_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "SESSIONS_DIR", tmp_path / "nope")
    assert registry.live_sessions() == []


def test_exclude_pids(sessions_dir):
    _write(sessions_dir, ALIVE)
    assert registry.owners_of(SID, exclude_pids=frozenset({ALIVE})) == []


# ---------------------------------------------------------------------------
# Recycled pids: a crashed claude's record outlives it, and the OS hands that
# number to something unrelated. Believing the record would give an innocent
# process ownership of the session and block control forever.
# ---------------------------------------------------------------------------

def test_recycled_pid_does_not_inherit_ownership(sessions_dir, monkeypatch):
    _write(sessions_dir, ALIVE)
    # The pid is alive — but it is no longer a claude.
    monkeypatch.setattr(registry, "claude_processes", lambda: {})
    assert registry.owners_of(SID) == []


def test_ps_failure_falls_back_to_liveness_not_to_empty(sessions_dir, monkeypatch):
    """Failing open would let a duplicate be spawned, so a broken `ps` must
    degrade to the plain liveness check rather than retiring every record."""
    _write(sessions_dir, ALIVE)
    monkeypatch.setattr(registry, "claude_processes", lambda: None)
    assert [o.pid for o in registry.owners_of(SID)] == [ALIVE]


# ---------------------------------------------------------------------------
# The blind spot: a claude that inherited CLAUDE_* env registers nothing, and
# as a plain `claude` carries no session flag either.
# ---------------------------------------------------------------------------

def _procs(**pid_to_argv):
    return {int(p): a for p, a in pid_to_argv.items()}


def test_unregistered_claude_in_cwd_is_reported(sessions_dir, monkeypatch):
    monkeypatch.setattr(registry, "claude_processes", lambda: _procs(**{"4242": "claude"}))
    monkeypatch.setattr(registry, "_cwds_of", lambda pids: {4242: "/proj/a"})
    found = registry.unidentified_claudes("/proj/a")
    assert [u.pid for u in found] == [4242]
    assert "4242" in found[0].describe()


def test_unregistered_claude_elsewhere_is_not_our_problem(sessions_dir, monkeypatch):
    monkeypatch.setattr(registry, "claude_processes", lambda: _procs(**{"4242": "claude"}))
    monkeypatch.setattr(registry, "_cwds_of", lambda pids: {4242: "/proj/other"})
    assert registry.unidentified_claudes("/proj/a") == []


def test_registered_claude_is_not_unidentified(sessions_dir, monkeypatch):
    _write(sessions_dir, ALIVE)
    monkeypatch.setattr(
        registry, "claude_processes", lambda: {ALIVE: "claude"}
    )
    monkeypatch.setattr(registry, "_cwds_of", lambda pids: {ALIVE: "/proj/a"})
    assert registry.unidentified_claudes("/proj/a") == []


def test_argv_identifiable_claude_is_left_to_the_argv_scan(sessions_dir, monkeypatch):
    monkeypatch.setattr(
        registry,
        "claude_processes",
        lambda: _procs(**{"4242": f"claude --resume {SID}"}),
    )
    monkeypatch.setattr(registry, "_cwds_of", lambda pids: {4242: "/proj/a"})
    assert registry.unidentified_claudes("/proj/a") == []


def test_embedded_runtime_is_not_an_interactive_owner(sessions_dir, monkeypatch):
    monkeypatch.setattr(
        registry,
        "claude_processes",
        lambda: _procs(**{"4242": "claude -p --output-format json"}),
    )
    monkeypatch.setattr(registry, "_cwds_of", lambda pids: {4242: "/proj/a"})
    assert registry.unidentified_claudes("/proj/a") == []


def test_ps_failure_reports_no_unidentified_claudes(monkeypatch):
    monkeypatch.setattr(registry, "claude_processes", lambda: None)
    assert registry.unidentified_claudes("/proj/a") == []
