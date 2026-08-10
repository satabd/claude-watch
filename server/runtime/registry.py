"""Claude Code's own live-session registry: ``~/.claude/sessions/<pid>.json``.

Every running ``claude`` writes one of these and heartbeats it:

    {"pid": 8353, "sessionId": "4db641a9-…", "cwd": "/…/claude-watch",
     "kind": "interactive", "entrypoint": "cli", "version": "2.1.220",
     "status": "idle", "updatedAt": 1786323786066, …}

This is *authoritative* identification, and it replaces guessing from ``ps``
argv. The argv scan can only recognise a process launched with ``--resume``
or ``--session-id``; a plain ``claude`` in a terminal picks its session id at
startup and carries no flag, so the scan reported "nobody owns this session"
for the most common case there is. Acting on that, ``ensure_managed`` would
start a *second* ``claude --resume <same id>`` — two interactive processes
appending to one JSONL, and a pane that replays the conversation instead of
mirroring it. That is the bug this module exists to prevent.

Nothing here is trusted blindly: a record is only believed when its pid is
still alive (records outlive crashes) and, for the busy/idle field, when the
heartbeat is fresh.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("watcher.runtime.registry")

SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# `status` is heartbeated; past this age treat it as unknown rather than
# stale-but-plausible. The pid liveness check still stands on its own.
STATUS_FRESH_S = 120.0

# `entrypoint` values that are not a terminal we could ever take over. A
# Claude Desktop session has no TTY to free, and killing it would sever a
# conversation happening in another app's window.
NON_TERMINAL_ENTRYPOINTS = frozenset({"claude-desktop", "sdk", "api"})


@dataclass(frozen=True)
class LiveSession:
    pid: int
    session_id: str
    cwd: str | None
    kind: str | None
    entrypoint: str | None
    status: str | None  # "idle" | "busy" | None when stale/absent
    version: str | None

    @property
    def takeoverable(self) -> bool:
        """True when this is an interactive terminal we could replace."""
        return (
            self.kind == "interactive"
            and (self.entrypoint or "cli") not in NON_TERMINAL_ENTRYPOINTS
        )

    @property
    def busy(self) -> bool | None:
        """Tri-state: True/False from a fresh heartbeat, None when unknown."""
        if self.status == "busy":
            return True
        if self.status == "idle":
            return False
        return None

    def describe(self) -> str:
        where = self.entrypoint or "cli"
        return f"{where} claude (pid {self.pid})"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _parse(path: Path, now: float) -> LiveSession | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid, sid = obj.get("pid"), obj.get("sessionId")
    if not isinstance(pid, int) or not isinstance(sid, str) or not sid:
        return None
    # The filename is the pid; a mismatch means the file was rewritten by
    # something else and the record can't be trusted to describe `pid`.
    if path.stem.isdigit() and int(path.stem) != pid:
        return None
    if not _pid_alive(pid):
        return None

    status = obj.get("status")
    updated = obj.get("updatedAt")
    if not isinstance(updated, (int, float)) or now - updated / 1000.0 > STATUS_FRESH_S:
        status = None  # heartbeat too old to act on

    return LiveSession(
        pid=pid,
        session_id=sid,
        cwd=obj.get("cwd"),
        kind=obj.get("kind"),
        entrypoint=obj.get("entrypoint"),
        status=status if isinstance(status, str) else None,
        version=obj.get("version"),
    )


def live_sessions() -> list[LiveSession]:
    """Every claude process currently registered and actually running."""
    try:
        files = list(SESSIONS_DIR.glob("*.json"))
    except OSError:
        return []
    now = time.time()
    out = []
    for f in files:
        if rec := _parse(f, now):
            out.append(rec)
    return out


def owners_of(session_id: str, *, exclude_pids: frozenset[int] = frozenset()) -> list[LiveSession]:
    """Live claude processes driving `session_id`, newest pid last.

    More than one is not impossible — it is exactly the broken state this
    module is meant to make visible — so this returns a list rather than
    pretending there is a single owner.
    """
    return sorted(
        (
            s
            for s in live_sessions()
            if s.session_id == session_id and s.pid not in exclude_pids
        ),
        key=lambda s: s.pid,
    )
