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

Nothing here is trusted blindly: a record is only believed when the pid is
still alive *and still a claude* (records outlive crashes, and a recycled pid
would otherwise hand ownership to an unrelated process), and, for the
busy/idle field, when the heartbeat is fresh.

The registry is authoritative but not complete. A ``claude`` that inherits
``CLAUDE_*`` environment from a parent Claude Code session writes no record at
all — the same env leak that makes it disable transcript saving. Such a
process owns a session that nothing here can name, so ``unidentified_claudes``
reports it by cwd and the caller treats the session as occupied rather than
free. Being wrong in that direction costs a confirmation click; being wrong in
the other direction corrupts a transcript.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
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


SUBPROCESS_TIMEOUT_S = 5.0

# Argv markers that mean "not an interactive terminal claude" — an embedded
# or headless runtime we must never count as a takeoverable owner.
_EMBEDDED_MARKERS = ("--output-format", "--print", "Claude.app", "claude-code/")
# A process launched with an explicit session flag is identifiable from argv
# alone, so the argv scan already covers it; only flagless claudes are the
# blind spot this module has to compensate for.
_SESSION_FLAG_RE = re.compile(r"--(?:resume|session-id)[=\s]")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def claude_processes() -> dict[int, str] | None:
    """``{pid: argv}`` for every running claude binary.

    None — not ``{}`` — when ``ps`` could not be run. The distinction matters:
    an empty result means "no claude is running" and would retire every
    record, which is the one direction that lets a duplicate be spawned. The
    callers fall back to a plain liveness check instead.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    procs: dict[int, str] = {}
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_s, cmd = line.split(None, 1)
            pid = int(pid_s)
        except ValueError:
            continue
        # `ps` gives argv as one string and macOS paths contain spaces
        # ("Application Support"), so take everything before the first flag
        # as the exe path and require it to END with the claude binary.
        exe = cmd.split(" -", 1)[0].strip()
        if exe == "claude" or exe.endswith("/claude"):
            procs[pid] = cmd
    return procs


def _cwds_of(pids: list[int]) -> dict[int, str]:
    """Working directory per pid, via ``lsof``. Missing entries are omitted.

    macOS `ps` cannot report another process's cwd, and claude does not hold
    its transcript open, so this is the only way to tell which project an
    otherwise-anonymous claude is sitting in.
    """
    if not pids:
        return {}
    try:
        out = subprocess.run(
            ["lsof", "-a", "-p", ",".join(str(p) for p in pids), "-d", "cwd", "-Fpn"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    # lsof exits non-zero when *any* pid is unreadable; stdout is still good
    # for the ones that worked, so returncode is deliberately ignored.
    cwds: dict[int, str] = {}
    pid: int | None = None
    for line in out.stdout.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid is not None:
            cwds[pid] = line[1:]
    return cwds


def _parse(
    path: Path, now: float, claude_pids: frozenset[int] | None = None
) -> LiveSession | None:
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
    # A record left behind by a crash keeps its pid, and the OS eventually
    # hands that number to something else. Requiring the pid to still BE a
    # claude retires the stale record instead of letting an unrelated process
    # inherit ownership of the session and block control forever.
    if claude_pids is not None:
        if pid not in claude_pids:
            return None
    elif not _pid_alive(pid):
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
    procs = claude_processes()
    claude_pids = None if procs is None else frozenset(procs)
    now = time.time()
    out = []
    for f in files:
        if rec := _parse(f, now, claude_pids):
            out.append(rec)
    return out


@dataclass(frozen=True)
class UnidentifiedClaude:
    """A live interactive claude that no registry record accounts for."""

    pid: int
    cwd: str | None

    def describe(self) -> str:
        return f"an unregistered claude (pid {self.pid})"


def unidentified_claudes(cwd: str | None = None) -> list[UnidentifiedClaude]:
    """Interactive claudes running that the registry does not describe.

    These are the processes that make "no registered owner" mean "unknown"
    rather than "free": a claude which inherited ``CLAUDE_*`` env writes no
    record, and if it was started as a plain ``claude`` it carries no session
    flag either, so nothing can name the session it is driving. All we can
    establish is which directory it is in — enough to refuse to spawn a second
    claude on a session in that same directory.

    Filtered to `cwd` when given. Processes carrying an explicit session flag
    are excluded: those are identifiable from argv, so the caller's argv scan
    resolves them precisely instead of by this blunt directory match.
    """
    procs = claude_processes()
    if not procs:  # None (ps failed) or genuinely nothing running
        return []
    registered = {rec.pid for rec in live_sessions()}
    candidates = [
        pid
        for pid, argv in procs.items()
        if pid not in registered
        and not _SESSION_FLAG_RE.search(argv)
        and not any(m in argv for m in _EMBEDDED_MARKERS)
    ]
    if not candidates:
        return []
    cwds = _cwds_of(candidates)
    out = [UnidentifiedClaude(pid=pid, cwd=cwds.get(pid)) for pid in sorted(candidates)]
    if cwd is None:
        return out
    target = os.path.realpath(cwd)
    return [
        u for u in out if u.cwd is not None and os.path.realpath(u.cwd) == target
    ]


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
