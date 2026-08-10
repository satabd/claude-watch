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
