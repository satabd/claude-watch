"""Ownership state machine for Claude sessions.

See CLAUDE.md. The short version:

* ``session_id`` is the identity. A pid, a Zellij session and a pane are
  *attachments* to it — disposable, re-verified on every use, never identity.

* A session is **managed** when claude-watch started the claude that is
  running it. That is a recorded fact, not an inference: the pid goes into
  ``runtime_bindings`` at spawn time, and "still managed" means that exact pid
  is still running and still a claude.

* A session is **external** when some other claude is alive on it. External
  sessions are strictly view-only. claude-watch does not take them over and
  does not signal them. When their process exits they become resumable, and
  resuming makes them managed.

* The invariant everything serves: **only one claude may write to a transcript
  at a time.** When we cannot prove the other process is gone, we refuse.

Ownership is decided from deterministic signals only — Claude Code's session
registry, an argv scan, and Zellij's own view of what exists. Screen scraping
(``parse_status``, ``parse_blocking_dialog``) is advisory: it drives the
spinner, the mode badge and the dialog buttons, and is never the basis for
starting or ending a process.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import db
from . import registry, zellij

_log = logging.getLogger("watcher.runtime")

# JSONL must be untouched for this long before "idle" is even considered.
IDLE_AFTER_S = 10.0
TERMINATE_WAIT_S = 8.0

# --resume for resumed sessions; --session-id when a runtime starts a fresh
# session with an explicit id (Claude Desktop / SDK launches). Either one
# positively ties a process to a session id.
_RESUME_RE = re.compile(r"--(?:resume|session-id)[=\s]+([0-9a-fA-F-]{8,})")

# Argv markers that mean "not an interactive terminal claude".
_EMBEDDED_MARKERS = ("--output-format", "--print", "Claude.app", "claude-code/")

# A numbered option row in a claude TUI dialog: "❯ 1. Yes" / "  2. No…"
_DIALOG_OPTION_RE = re.compile(r"^\s*(?:❯\s*)?(\d+)\.\s+(.*\S)\s*$")

# The TUI status line always begins with a mode glyph: "⏸" (asks first) or
# "⏵⏵" (proceeds on its own). Anchoring on it means a numbered list or the
# word "plan" in ordinary assistant output can never be mistaken for a mode.
_STATUS_LINE_RE = re.compile(r"^\s*(⏸|⏵⏵)\s*(.+)$")

# Mode phrases as rendered, most specific first. The trailing " on" is
# deliberately NOT required: a narrow pane truncates the line (a real
# bypass-mode pane renders just "⏵⏵ bypass         ·"), and losing the mode
# because the window is narrow would be worse than matching on the stem.
_MODE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("bypass", re.compile(r"\bbypass\b", re.I)),
    ("dont_ask", re.compile(r"\bdon['’]?t ask\b", re.I)),
    ("accept_edits", re.compile(r"\baccept edits\b", re.I)),
    ("auto", re.compile(r"\bauto\b", re.I)),
    ("plan", re.compile(r"\bplan\b", re.I)),
    ("manual", re.compile(r"\bmanual\b", re.I)),
]
MODE_LABELS = {
    "manual": "Manual",
    "accept_edits": "Accept edits",
    "plan": "Plan",
    "auto": "Auto",
    "dont_ask": "Don't ask",
    "bypass": "Bypass permissions",
}
# `--permission-mode` spelling for each of our snake_case keys.
MODE_CLI_FLAGS = {
    "manual": "manual",
    "accept_edits": "acceptEdits",
    "plan": "plan",
    "auto": "auto",
    "dont_ask": "dontAsk",
    "bypass": "bypassPermissions",
}
# Sessions claude-watch starts run in Auto: Claude picks the right permission
# behaviour per action instead of stopping on every edit, which is what makes
# a pane driven from a phone/browser usable.
DEFAULT_PERMISSION_MODE = "auto"
# Which modes Shift+Tab visits is build- and settings-dependent — `auto` is in
# the cycle on 2.1.x but was not on older builds. Rather than hardcode a list
# and refuse everything outside it, set_mode() cycles and re-reads, and only
# gives up once it has seen the whole loop repeat. `bypass` stays excluded:
# it is never in the cycle, and silently landing there would be unsafe.
UNREACHABLE_BY_CYCLING = ("bypass",)
# Shift+Tab — the key the TUI uses to cycle permission modes.
BACKTAB = [27, 91, 90]  # ESC [ Z

# Active spinner: "✽ Lollygagging… (5s · thought for 1s)". The trailing
# "(Ns …)" is what distinguishes a RUNNING spinner from a finished one
# ("✻ Cooked for 2s"), which stays on screen as history.
_SPINNER_RE = re.compile(r"([A-Za-z]+)…\s*\((\d+)s([^)]*)\)")
# The status line swaps "? for shortcuts" for "esc to interrupt" while a
# turn is in flight — the most reliable "is it working" signal.
_WORKING_MARKER = "esc to interrupt"


def parse_status(screen: str) -> dict:
    """Extract {mode, working, activity} from a rendered pane.

    All fields are best-effort: a narrow pane truncates the status line, and
    the caller must treat every value as optional.
    """
    lines = screen.splitlines()
    tail = lines[-12:]

    mode = None
    for ln in reversed(tail):
        sm = _STATUS_LINE_RE.match(ln)
        if not sm:
            continue
        body = sm.group(2)
        for key, pat in _MODE_PATTERNS:
            if pat.search(body):
                mode = key
                break
        if mode:
            break

    working = any(_WORKING_MARKER in ln for ln in tail)

    activity = None
    for ln in reversed(tail):
        m = _SPINNER_RE.search(ln)
        if m:
            activity = {
                "verb": m.group(1),
                "elapsed_s": int(m.group(2)),
                "detail": m.group(3).strip(" ·") or None,
            }
            working = True  # a live spinner is proof of work in flight
            break

    return {"mode": mode, "working": working, "activity": activity}


@dataclass
class RuntimeState:
    """What the UI needs to render the composer for one session."""

    state: str  # managed | external_idle | external_busy | inactive | resumable
    controllable: bool
    reason: str | None = None  # why not controllable / extra context
    zellij_session: str | None = None
    pane_id: str | None = None
    # `<project>-<session>` as rendered in zellij's tab and pane title.
    pane_title: str | None = None
    zellij_tab: str | None = None
    external_pid: int | None = None
    busy: bool = False
    # Set when the managed TUI is blocked on an interactive dialog
    # (permission prompt, trust prompt, question with numbered options):
    # {"question": str, "options": [{"n": "1", "label": "Yes"}, ...]}
    awaiting_input: dict | None = None
    # Permission mode of the managed TUI: manual | accept_edits | plan | bypass
    mode: str | None = None
    # True while a turn is in flight (read from the live pane, not the JSONL).
    working: bool = False
    # Live spinner: {"verb": "Lollygagging", "elapsed_s": 5, "detail": "thinking"}
    activity: dict | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "controllable": self.controllable,
            "reason": self.reason,
            "zellij_session": self.zellij_session,
            "pane_id": self.pane_id,
            "pane_title": self.pane_title,
            "zellij_tab": self.zellij_tab,
            # Ready to paste in a terminal to watch the very same TUI.
            "attach_command": (
                f"zellij attach {self.zellij_session}"
                if self.zellij_session
                else None
            ),
            "external_pid": self.external_pid,
            "busy": self.busy,
            "awaiting_input": self.awaiting_input,
            "mode": self.mode,
            "mode_label": MODE_LABELS.get(self.mode or "", None),
            "working": self.working,
            "activity": self.activity,
            **({"detail": self.detail} if self.detail else {}),
        }


_BOX_CHARS = "│┃┆┊╎║"
_LEADING_BORDER_RE = re.compile(rf"^[\s{_BOX_CHARS}]*[{_BOX_CHARS}]")


def _strip_box(line: str) -> str:
    """Drop claude's box-drawing frame from both ends of a line.

    Claude renders some dialogs inside a bordered box, so option rows arrive
    as "│ ❯ 1. Yes                     │". Without stripping the frame the
    option regex never matches and the dialog reads as "no dialog" — which is
    what left the UI showing "working" forever.

    Indentation *inside* the frame is deliberately preserved: it is the only
    thing distinguishing a wrapped option label ("     (shift+tab)") from the
    next unindented line of chrome.
    """
    s = _LEADING_BORDER_RE.sub("", line.rstrip())
    return s.rstrip(_BOX_CHARS + " \t")


# Hints claude prints under a blocking dialog. Any one of them is enough;
# which appears depends on the dialog kind (permission / trust / plan).
_DIALOG_HINTS = (
    "esc to cancel",
    "enter to confirm",
    "tab to amend",
)
# A selected option row: "❯ 1. Yes". The bare "❯" of the composer must not
# count — it is on screen at all times.
_SELECTED_OPTION_RE = re.compile(r"^❯\s*\d+\.")
# Lines that are chrome, not the question.
_DECORATION_PREFIXES = ("─", "╌", "│", "╭", "╰", "━", "┃", "·")


def parse_blocking_dialog(screen: str) -> dict | None:
    """Detect a claude TUI dialog awaiting a numbered choice.

    Reports only when the viewport tail shows at least two numbered options
    *and* either one of claude's dialog hint lines or a "❯"-marked selection
    row — an assistant reply that merely contains a numbered list has
    neither. Continuation lines (wrapped option text with no number) fold
    into the previous option's label.

    The question is the nearest line above the first option that ends in "?";
    that beats "nearest non-decoration line", which on the real trust dialog
    picks up the "Security guide" footer link sitting between the prose and
    the options.
    """
    # 40, not 25: at the geometry we force (see zellij.DEFAULT_ROWS) a
    # permission dialog with a diff preview pushes its hint line well past
    # 25 rows from the bottom.
    lines = [_strip_box(ln) for ln in screen.splitlines()[-40:]]
    lowered = [ln.lower() for ln in lines]
    has_hint = any(h in ln for ln in lowered for h in _DIALOG_HINTS)
    has_marker = any(_SELECTED_OPTION_RE.match(ln.strip()) for ln in lines)
    if not (has_hint or has_marker):
        return None

    options: list[dict] = []
    first_option_idx: int | None = None
    for i, ln in enumerate(lines):
        m = _DIALOG_OPTION_RE.match(ln)
        if m:
            options.append({"n": m.group(1), "label": m.group(2)})
            if first_option_idx is None:
                first_option_idx = i
            continue
        if not options:
            continue
        # A wrapped label is indented under its option row. Anything flush
        # left (the hint line, the status line, the next chunk of output) or
        # blank ends the option block, so it can never be glued onto the
        # last option's label.
        s = ln.strip()
        if not s or not ln.startswith((" ", "\t")):
            break
        low = s.lower()
        if s.startswith(_DECORATION_PREFIXES) or any(h in low for h in _DIALOG_HINTS):
            break
        options[-1]["label"] += " " + s
    if len(options) < 2:
        return None

    # The question is a *paragraph*, not a line: claude hard-wraps its prose
    # to the pane width, so "Is this a project you / created or one you
    # trust? ..." arrives as five lines. Paragraphs are runs of non-blank,
    # non-decoration lines — the "╌╌╌" rule under a diff preview is what
    # keeps the diff itself out of the question.
    paragraphs: list[list[str]] = []
    for ln in lines[: first_option_idx or 0]:
        s = ln.strip()
        if not s or s.startswith(_DECORATION_PREFIXES):
            paragraphs.append([])
            continue
        if not paragraphs:
            paragraphs.append([])
        paragraphs[-1].append(s)
    candidates = [" ".join(p) for p in paragraphs if p]
    question = next(
        (c for c in reversed(candidates) if "?" in c),
        candidates[-1] if candidates else None,
    )
    return {"question": question or "Claude is asking for input", "options": options}


# ---------------------------------------------------------------------------
# Naming: one zellij session per project, one tab/pane per claude session
# ---------------------------------------------------------------------------

# Zellij session names end up in `zellij attach <name>`, so keep them to
# shell-safe characters; anything else collapses to a single dash.
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(text: str, limit: int = 32) -> str:
    s = _UNSAFE_NAME_RE.sub("-", text.strip()).strip("-._").lower()
    return s[:limit].rstrip("-._")


def project_name(cwd: str | None, bucket: str | None = None) -> str:
    """The zellij session name for a project — its folder name ("rumailahub").

    Falls back to the last meaningful segment of the encoded bucket when the
    transcript carries no cwd, and finally to a constant so we never produce
    an empty (and therefore un-attachable) session name.
    """
    if cwd:
        base = Path(cwd).name or Path(cwd).parent.name
        if slug := _slug(base):
            return slug
    if bucket:
        # Buckets are cwds with separators flattened to "-"; the tail segment
        # is the folder name often enough to be a useful label.
        tail = bucket.rstrip("-").split("-")[-1]
        if slug := _slug(tail):
            return slug
    return "claude-watch"


def session_label(session_id: str, title: str | None = None) -> str:
    """Human-facing name of one claude session inside its project."""
    if title and (slug := _slug(title, 40)):
        return slug
    return session_id[:8]


def pane_title(session_id: str, cwd: str | None, title: str | None = None) -> str:
    """`<project>-<session>` — the tab and pane name the user sees in zellij."""
    return f"{project_name(cwd)}-{session_label(session_id, title)}"


def legacy_session_name(session_id: str) -> str:
    """Pre-project naming. Still adopted so upgrades don't orphan a runtime."""
    return f"cw-{session_id[:8]}"


# Kept as the historical entry point used by tests and older call sites.
def _pid_is_live_claude(pid: int) -> bool:
    """True when `pid` is running AND is still a claude.

    Liveness alone is not enough: pids get recycled, and signalling a
    recycled pid would kill an unrelated process.
    """
    procs = registry.claude_processes()
    if procs is None:  # `ps` unavailable — fall back to bare liveness
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return pid in procs


def zellij_session_name(session_id: str, cwd: str | None = None) -> str:
    return project_name(cwd) if cwd else legacy_session_name(session_id)


# ---------------------------------------------------------------------------
# Process discovery
# ---------------------------------------------------------------------------

@dataclass
class ClaudeProc:
    pid: int
    argv: str
    resume_id: str | None
    embedded: bool


def _ps_lines() -> list[str]:
    """Raw `ps` output; isolated for test monkeypatching."""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.splitlines()
    except Exception:  # pragma: no cover - ps missing is effectively fatal
        return []


def find_claude_processes(lines: list[str] | None = None) -> list[ClaudeProc]:
    procs: list[ClaudeProc] = []
    for line in lines if lines is not None else _ps_lines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_s, cmd = line.split(None, 1)
            pid = int(pid_s)
        except ValueError:
            continue
        # The executable itself must be claude — not grep, this server, or
        # claude-watch. `ps` gives argv as one string and macOS paths contain
        # spaces ("Application Support"), so take everything before the first
        # flag as the exe path and require it to END with the claude binary.
        exe_part = cmd.split(" -", 1)[0].strip()
        if not (exe_part == "claude" or exe_part.endswith("/claude")):
            continue
        m = _RESUME_RE.search(cmd)
        embedded = any(marker in cmd for marker in _EMBEDDED_MARKERS)
        procs.append(
            ClaudeProc(
                pid=pid,
                argv=cmd,
                resume_id=m.group(1) if m else None,
                embedded=embedded,
            )
        )
    return procs


# ---------------------------------------------------------------------------
# Idle detection (heuristic, deliberately conservative)
# ---------------------------------------------------------------------------

def transcript_looks_terminal(jsonl_path: Path, tail_lines: int = 60) -> bool:
    """True when the transcript's tail shape suggests Claude finished a turn.

    Busy signals: last meaningful event is a user prompt (answer pending) or
    an assistant tool_use with no tool_result yet (tool running).
    """
    try:
        with jsonl_path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    lines = chunk.splitlines()[-tail_lines:]

    pending_tool_ids: set[str] = set()
    last_role: str | None = None
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        if t not in ("user", "assistant"):
            continue
        content = (obj.get("message") or {}).get("content")
        if t == "assistant":
            last_role = "assistant"
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_use":
                        pending_tool_ids.add(blk.get("id") or "")
        else:
            if isinstance(content, list):
                got_result = False
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        pending_tool_ids.discard(blk.get("tool_use_id") or "")
                        got_result = True
                # A real typed prompt (not a tool result) => answer pending
                last_role = "assistant" if got_result else "user"
            else:
                last_role = "user"
    if last_role == "user":
        return False
    if pending_tool_ids:
        return False
    # No user/assistant turns at all (fresh or metadata-only session): there
    # is nothing that could be "mid-turn", so the shape is terminal — without
    # this, turn-less sessions read as busy forever and become uncontrollable.
    return last_role in ("assistant", None)


def session_is_busy(jsonl_path: Path) -> bool:
    try:
        age = time.time() - jsonl_path.stat().st_mtime
    except OSError:
        return False
    if age < IDLE_AFTER_S:
        return True
    return not transcript_looks_terminal(jsonl_path)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class ClaudeRuntimeController:
    def __init__(self) -> None:
        # Serialize control mutations per session to kill duplicate-request races.
        self._locks: dict[str, asyncio.Lock] = {}
        # Serialize tab creation *per zellij session*: two claude sessions in
        # the same project would otherwise both diff `list-panes` around each
        # other's new pane and could not tell which one is theirs.
        self._zellij_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    def _zellij_lock_for(self, name: str) -> asyncio.Lock:
        if name not in self._zellij_locks:
            self._zellij_locks[name] = asyncio.Lock()
        return self._zellij_locks[name]

    # -- state ---------------------------------------------------------------

    async def get_state(
        self,
        session_id: str,
        jsonl_path: Path,
        *,
        remote_name: str | None = None,
        cwd: str | None = None,
        title: str | None = None,
    ) -> RuntimeState:
        if remote_name:
            return RuntimeState(
                state="inactive",
                controllable=False,
                reason=f"Session lives on remote host '{remote_name}'; "
                "control is local-only for now.",
            )
        if sys.platform == "win32":
            return RuntimeState(
                state="inactive",
                controllable=False,
                reason="Zellij has no native Windows build. Run claude-watch "
                "inside WSL to control sessions.",
            )
        try:
            zellij.zellij_bin()
        except zellij.ZellijUnavailable as e:
            return RuntimeState(state="inactive", controllable=False, reason=str(e))

        busy = session_is_busy(jsonl_path)

        # Who is actually alive for this session? Deterministic sources only:
        # Claude Code's registry, then an argv scan for `--resume <id>`. No
        # pane titles, nothing off a screen.
        live = self._live_pids_for(session_id)
        binding = db.runtime_binding_get(session_id)

        # --- MANAGED: a pid we wrote down, still running, still a claude ----
        if binding:
            ours = self._resolve_binding_pid(session_id, binding, live)
            if ours is not None:
                return await self._managed_state(session_id, binding, ours, busy)
            # The binding does not describe a live process of ours. It is an
            # attachment, not identity, so drop it and re-decide from scratch.
            _log.info("retiring runtime binding for %s (no live pid)", session_id)
            db.runtime_binding_delete(session_id)

        # --- EXTERNAL: someone else's claude is alive on this transcript ----
        if live:
            owner = live[-1]
            if len(live) > 1:
                pids = ", ".join(str(o.pid) for o in live)
                _log.warning(
                    "session %s has %d live claude processes (pids %s)",
                    session_id, len(live), pids,
                )
                return RuntimeState(
                    state="external_busy",
                    controllable=False,
                    external_pid=owner.pid,
                    busy=True,
                    reason=f"{len(live)} claude processes (pids {pids}) are "
                    "writing to this transcript at once, so neither view is "
                    "complete. Close all but one.",
                    detail={"owner_pids": [o.pid for o in live]},
                )
            effective_busy = busy if owner.busy is None else owner.busy
            return RuntimeState(
                state="external_busy" if effective_busy else "external_idle",
                controllable=False,
                external_pid=owner.pid,
                busy=effective_busy,
                reason=f"This session is running outside claude-watch "
                f"({owner.describe()}). It is view-only while that process is "
                "alive. Close it and you can resume the session here.",
            )

        # --- Nothing owns it by name, but something may still be writing ----
        # A claude that inherited CLAUDE_* env registers nothing and, started
        # as a plain `claude`, carries no session flag either. Only its
        # directory is knowable. That is not enough to call it the owner — but
        # it is enough to refuse to start a second process on this transcript.
        if unknown := registry.unidentified_claudes(cwd):
            pids = ", ".join(str(u.pid) for u in unknown)
            _log.info(
                "session %s: %d unregistered claude(s) in %s (pids %s)",
                session_id, len(unknown), cwd, pids,
            )
            return RuntimeState(
                state="external_idle",
                controllable=False,
                busy=busy,
                reason=(
                    f"An unregistered claude (pid {pids}) is running in this "
                    "project. It publishes no session id, so it cannot be "
                    "ruled out as the owner of this session, and resuming "
                    "anyway could put two claudes on one transcript. Close it "
                    "to resume here."
                ),
                detail={"unregistered_pids": [u.pid for u in unknown]},
            )

        # --- RESUMABLE: no claude is alive for this session -----------------
        # The transcript may still end mid-turn if the process was killed
        # mid-write; that is a reason to say so, not a reason to refuse.
        return RuntimeState(
            state="resumable" if busy else "inactive",
            controllable=True,
            busy=busy,
            reason=(
                "The previous claude exited mid-turn. Resuming starts a fresh "
                "process on this transcript."
                if busy
                else None
            ),
        )

    # -- ownership resolution ------------------------------------------------

    def _live_pids_for(self, session_id: str) -> list[registry.LiveSession]:
        """Every live claude that can be *positively tied* to this session.

        Two deterministic sources, unioned: Claude Code's registry (names the
        session outright) and an argv scan for `--resume`/`--session-id`
        (which is how our own spawns are always identifiable, even if the
        registry misses them). Embedded/headless runtimes are included — they
        own the transcript just as much, and must block a resume — but they
        are never takeoverable.
        """
        found: dict[int, registry.LiveSession] = {
            rec.pid: rec for rec in registry.owners_of(session_id)
        }
        for proc in find_claude_processes():
            if proc.resume_id and session_id.startswith(proc.resume_id):
                found.setdefault(
                    proc.pid,
                    registry.LiveSession(
                        pid=proc.pid,
                        session_id=session_id,
                        cwd=None,
                        kind=None if proc.embedded else "interactive",
                        entrypoint="sdk" if proc.embedded else "cli",
                        status=None,
                        version=None,
                    ),
                )
        return [found[pid] for pid in sorted(found)]

    def _resolve_binding_pid(
        self,
        session_id: str,
        binding: dict,
        live: list[registry.LiveSession],
    ) -> int | None:
        """The pid this binding refers to, if it is still our live claude.

        Bindings written before pid was recorded carry NULL. Those are
        upgraded in place when exactly one live claude owns the session — the
        argv/registry match names the session, so there is no guessing — and
        retired otherwise.
        """
        live_pids = {rec.pid for rec in live}
        recorded = binding.get("pid")
        if recorded is not None:
            return recorded if recorded in live_pids else None
        if len(live) == 1:
            pid = live[0].pid
            db.runtime_binding_put(
                session_id,
                binding["zellij_session"],
                binding["pane_id"],
                cwd=binding.get("cwd"),
                tab_name=binding.get("tab_name"),
                pid=pid,
            )
            _log.info("adopted pid %s into legacy binding for %s", pid, session_id)
            return pid
        return None

    async def _managed_state(
        self, session_id: str, binding: dict, pid: int, busy: bool
    ) -> RuntimeState:
        """Render a managed session. The pane is an attachment: if it is gone
        the session is still ours, it just needs one rebuilt."""
        name, pane = binding["zellij_session"], binding["pane_id"]
        pane_alive = False
        panes: list[tuple[str, str, str]] = []
        if await zellij.session_state(name) == "alive":
            panes = await zellij.list_panes(name)
            pane_alive = any(p[0] == pane for p in panes)
        if not pane_alive:
            return RuntimeState(
                state="managed",
                controllable=True,
                zellij_session=name,
                pane_id=None,
                pane_title=binding.get("tab_name"),
                zellij_tab=binding.get("tab_name"),
                external_pid=pid,
                busy=busy,
                reason="The claude for this session is running but its pane is "
                "gone. Reattach to get a window on it again.",
            )

        db.runtime_binding_touch(session_id)
        awaiting = None
        status: dict = {}
        try:
            screen = await zellij.dump_screen(name, pane)
            awaiting = parse_blocking_dialog(screen)
            status = parse_status(screen)
        except zellij.ZellijError:
            pass  # advisory only — never load-bearing
        # A pane blocked on a dialog often keeps its spinner and "esc to
        # interrupt" on screen, which is what made a session asking a question
        # look permanently stuck. The question always wins.
        working = bool(status.get("working")) and awaiting is None
        return RuntimeState(
            state="managed",
            controllable=True,
            zellij_session=name,
            pane_id=pane,
            pane_title=next((p[2] for p in panes if p[0] == pane), None),
            zellij_tab=binding.get("tab_name"),
            external_pid=pid,
            busy=(working or busy) and awaiting is None,
            awaiting_input=awaiting,
            mode=status.get("mode"),
            working=working,
            activity=status.get("activity") if working else None,
        )

    # -- control -------------------------------------------------------------

    async def ensure_managed(
        self,
        session_id: str,
        jsonl_path: Path,
        cwd: str | None,
        *,
        remote_name: str | None = None,
        title: str | None = None,
    ) -> RuntimeState:
        """Make this session managed: resume it into a pane, or rebuild the
        pane of one we already own.

        Never takes a session away from a running claude. If anything else is
        alive on this transcript the caller gets a refusal — that is the whole
        contract, and the reason there is no `allow_takeover` any more.
        """
        async with self._lock_for(session_id):
            state = await self.get_state(
                session_id, jsonl_path, remote_name=remote_name, cwd=cwd, title=title
            )
            if not state.controllable:
                raise ControlRefused(state.reason or f"state={state.state}")
            if state.state == "managed":
                if state.pane_id is not None:
                    return state
                # Ours, but its window is gone. Give it a new one; the process
                # — and therefore the conversation — is untouched.
                return await self._reattach_pane(session_id, cwd, title, state)

            # Last-mile guard. get_state() ran before the awaits above, and a
            # `claude` can be started by hand in the seconds since. Starting a
            # second process on one transcript is the most damaging thing this
            # class can do, so re-ask immediately before committing.
            if survivors := self._live_pids_for(session_id):
                raise ControlRefused(
                    f"{survivors[-1].describe()} is alive on this session; "
                    "refusing to start a second claude on the same transcript"
                )
            if unknown := registry.unidentified_claudes(cwd):
                raise ControlRefused(
                    f"{unknown[0].describe()} is running in this project and "
                    "publishes no session id, so it cannot be ruled out as "
                    "this session's owner; refusing to start a second claude "
                    "on the same transcript"
                )

            # inactive / resumable → build the runtime.
            # One zellij session per project, one *tab* per claude session:
            # a tab gives the TUI the session's full width, where a second
            # pane in a shared tab would halve it and wreck dialog rendering.
            name = project_name(cwd)
            tab = pane_title(session_id, cwd, title)
            async with self._zellij_lock_for(name):
                await zellij.create_session(name)
                pane = await zellij.new_tab(
                    name,
                    tab,
                    cwd,
                    [
                        "claude",
                        "--resume",
                        session_id,
                        "--permission-mode",
                        DEFAULT_PERMISSION_MODE,
                    ],
                )
            try:
                await zellij.rename_pane(name, pane, tab)
            except zellij.ZellijError:
                pass  # cosmetic only
            # Don't hand the runtime out until the TUI is actually accepting
            # input — injecting into a booting claude loses the submit Enter.
            await self._wait_tui_ready(name, pane)
            # Write down which process is ours. Everything afterwards —
            # "is this still managed", "what may release() reap" — reads this
            # instead of re-deriving ownership from panes and titles.
            pid = self._spawned_pid(session_id)
            db.runtime_binding_put(
                session_id, name, pane, cwd=cwd, tab_name=tab, pid=pid
            )
            _log.info(
                "managed runtime created: session=%s zellij=%s tab=%s pane=%s pid=%s",
                session_id, name, tab, pane, pid,
            )
            return RuntimeState(
                state="managed",
                controllable=True,
                zellij_session=name,
                pane_id=pane,
                pane_title=tab,
                zellij_tab=tab,
                external_pid=pid,
                mode=DEFAULT_PERMISSION_MODE,
                busy=False,
            )

    def _spawned_pid(self, session_id: str) -> int | None:
        """The pid of the claude we just started, or None if it cannot be
        named yet. We always spawn with ``--resume <session_id>``, so the argv
        scan identifies it exactly; the registry is consulted too in case the
        process is slower to appear in one than the other. None is survivable:
        the binding is upgraded on the next state read."""
        live = self._live_pids_for(session_id)
        return live[-1].pid if live else None

    async def _reattach_pane(
        self,
        session_id: str,
        cwd: str | None,
        title: str | None,
        state: RuntimeState,
    ) -> RuntimeState:
        """Rebuild a window onto a claude we own whose pane has gone.

        No new claude process is started — the pane is an attachment, and this
        replaces the attachment only. `zellij attach` in a fresh tab would be
        wrong (it nests a client); instead the existing session is reported so
        the user can attach, and the binding keeps the pid.
        """
        name = project_name(cwd)
        tab = pane_title(session_id, cwd, title)
        _log.info(
            "session %s: pane gone, claude pid %s still alive",
            session_id, state.external_pid,
        )
        return RuntimeState(
            state="managed",
            controllable=False,
            zellij_session=name,
            pane_id=None,
            pane_title=tab,
            zellij_tab=tab,
            external_pid=state.external_pid,
            busy=state.busy,
            reason=f"claude (pid {state.external_pid}) is still running for "
            "this session but its pane is gone, so there is nothing to type "
            "into. Close it from Close pane, then resume here.",
        )

    async def _wait_tui_ready(
        self, name: str, pane: str, timeout: float = 15.0
    ) -> None:
        """Poll the pane until the claude composer prompt is on screen.

        The TUI takes a couple of seconds to boot after `claude --resume`;
        writing before that leaves the prompt text unsubmitted (the Enter is
        swallowed by the boot screen). Times out softly — worst case we're
        back to the old behavior.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                screen = await zellij.dump_screen(name, pane)
            except zellij.ZellijError:
                screen = ""
            # The composer row renders a "❯" prompt marker; the boot screen
            # and resume-picker don't.
            if "❯" in screen:
                return
            await asyncio.sleep(0.5)
        _log.warning("TUI readiness wait timed out for %s/%s", name, pane)

    async def _terminate_owned_pid(self, pid: int) -> None:
        """SIGTERM a claude **claude-watch started**; wait for it to exit.

        Only ever called with a pid read back out of our own runtime binding.
        There is deliberately no path from a user action to signalling a
        process we did not start — an external claude is view-only, and the
        way to end it is to close it where it lives.

        Never SIGKILL: a claude caught mid-write to its JSONL should be
        allowed to finish the line.
        """
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + TERMINATE_WAIT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
        raise ControlRefused(
            f"our claude (pid {pid}) did not exit within "
            f"{TERMINATE_WAIT_S:.0f}s (not killed with -9 on purpose — it may "
            "be mid-write)"
        )

    # -- prompt delivery -----------------------------------------------------

    async def send_prompt(self, session_id: str, prompt: str) -> None:
        """Inject prompt text and submit it. Caller must be in managed state."""
        async with self._lock_for(session_id):
            binding = db.runtime_binding_get(session_id)
            if not binding:
                raise ControlRefused("no managed runtime; take control first")
            ours = binding.get("pid")
            if ours is not None and not _pid_is_live_claude(ours):
                db.runtime_binding_delete(session_id)
                raise ControlRefused(
                    "the claude we started for this session has exited; "
                    "resume it here before sending"
                )
            name, pane = binding["zellij_session"], binding["pane_id"]
            if not pane:
                raise ControlRefused(
                    "this session has no pane to type into; resume it here first"
                )
            # verify liveness immediately before writing — stale bindings must
            # fail loudly rather than fall back to another session (zellij's
            # single-session fallback would type into an unrelated terminal).
            if await zellij.session_state(name) != "alive":
                db.runtime_binding_delete(session_id)
                raise ControlRefused(
                    "managed zellij session vanished; session is resumable — "
                    "take control again"
                )
            await zellij.type_text(name, pane, prompt)
            # tiny settle so the TUI registers the paste before Enter
            await asyncio.sleep(0.15)
            await zellij.submit(name, pane)

    async def release(self, session_id: str) -> dict:
        """Close the managed pane and make sure its claude is really gone.

        Closing the pane is the normal way out, but "the pane is gone" is not
        the same claim as "the process is gone", and the difference matters:
        a surviving claude keeps appending to the session's transcript from
        nowhere visible, and the next take-control then refuses because the
        registry — correctly — reports the session as owned.

        So this verifies the outcome and reaps a survivor with SIGTERM — but
        only the pid recorded in our own binding. "Whoever owns this session"
        is the wrong question here: if some other claude has since attached to
        the transcript, it is not ours to kill.
        """
        async with self._lock_for(session_id):
            binding = db.runtime_binding_get(session_id)
            if not binding:
                return {"released": False, "reason": "no managed runtime"}
            name, pane = binding["zellij_session"], binding["pane_id"]
            ours = binding.get("pid")

            if pane and await zellij.session_state(name) == "alive":
                try:
                    await zellij.close_pane(name, pane)
                except zellij.ZellijError as e:
                    _log.info("close_pane failed during release of %s: %s", session_id, e)

            # Give the TUI a moment to exit on its own before reaching for a
            # signal — a clean exit flushes; a signal races the flush.
            reaped: list[int] = []
            if ours is not None:
                deadline = time.monotonic() + 6.0
                while time.monotonic() < deadline:
                    if not _pid_is_live_claude(ours):
                        break
                    await asyncio.sleep(0.5)
                if _pid_is_live_claude(ours):
                    _log.warning(
                        "claude %s survived pane close for %s; terminating",
                        ours, session_id,
                    )
                    try:
                        await self._terminate_owned_pid(ours)
                        reaped.append(ours)
                    except ControlRefused as e:
                        _log.error("could not reap %s: %s", ours, e)

            db.runtime_binding_delete(session_id)
            still = [ours] if ours is not None and _pid_is_live_claude(ours) else []
            return {
                "released": True,
                "zellij_session": name,
                "pane_id": pane,
                "reaped_pids": reaped,
                "surviving_pids": still,
            }

    async def interrupt(self, session_id: str) -> None:
        binding = db.runtime_binding_get(session_id)
        if not binding:
            raise ControlRefused("no managed runtime")
        await zellij.send_escape(binding["zellij_session"], binding["pane_id"])

    async def set_mode(self, session_id: str, target: str) -> str:
        """Cycle the TUI's permission mode with Shift+Tab until `target`.

        There is no direct "set mode" key — Shift+Tab only cycles — so we
        press and re-read, at most one full lap plus slack. Verifying after
        every press means an unexpected cycle order (or a config with a
        bypass mode) still converges instead of guessing an index.
        """
        if target not in MODE_LABELS:
            raise ControlRefused(f"unknown mode {target!r}")
        if target in UNREACHABLE_BY_CYCLING:
            raise ControlRefused(
                f"{MODE_LABELS[target]} is a launch-time mode "
                "(--permission-mode); the TUI's Shift+Tab cycle cannot reach "
                "it. Restart the session with that flag to use it."
            )
        async with self._lock_for(session_id):
            binding = db.runtime_binding_get(session_id)
            if not binding:
                raise ControlRefused("no managed runtime; take control first")
            name, pane = binding["zellij_session"], binding["pane_id"]

            current = parse_status(await zellij.dump_screen(name, pane))
            if current.get("working"):
                raise ControlRefused(
                    "Claude is working — wait for the turn to finish before "
                    "changing mode."
                )
            if current.get("mode") == target:
                return target

            # Press-and-verify rather than counting presses: the cycle's
            # length and order differ between claude builds and settings, so
            # the stop condition is "target reached" and the give-up
            # condition is "the cycle repeated without passing through it".
            seen: list[str] = [current.get("mode") or "?"]
            for _ in range(8):
                await zellij.write_bytes(name, pane, BACKTAB)
                await asyncio.sleep(0.35)
                now = parse_status(await zellij.dump_screen(name, pane)).get("mode")
                if now == target:
                    _log.info("mode set to %s for %s", target, session_id)
                    return target
                if now and now in seen and len(seen) > 1:
                    break  # looped all the way round; target isn't in it
                seen.append(now or "?")
            raise ControlRefused(
                f"could not reach {MODE_LABELS[target]} mode by cycling — this "
                f"build's Shift+Tab loop is {' → '.join(seen)}. Restart the "
                f"session with --permission-mode {MODE_CLI_FLAGS[target]} to "
                "use it."
            )

    async def respond(self, session_id: str, choice: str) -> None:
        """Answer a blocking TUI dialog: a digit picks that option, "esc"
        cancels. Verified against the live dialog first so a stale click
        (dialog already gone) can't type stray digits into the composer."""
        async with self._lock_for(session_id):
            binding = db.runtime_binding_get(session_id)
            if not binding:
                raise ControlRefused("no managed runtime")
            name, pane = binding["zellij_session"], binding["pane_id"]
            screen = await zellij.dump_screen(name, pane)
            dialog = parse_blocking_dialog(screen)
            if not dialog:
                raise ControlRefused("no dialog is awaiting input (it may have "
                                     "been answered already)")
            if choice == "esc":
                await zellij.send_escape(name, pane)
                return
            if choice not in {o["n"] for o in dialog["options"]}:
                raise ControlRefused(f"choice {choice!r} is not one of the "
                                     "dialog's options")
            await zellij.write_chars(name, pane, choice)
            await asyncio.sleep(0.15)
            # Most dialogs act on the digit alone; Enter confirms the ones
            # that don't. On an already-closed dialog Enter is a no-op
            # (empty composer submit does nothing).
            await zellij.submit(name, pane)


class ControlRefused(RuntimeError):
    """The requested control action is unsafe or impossible right now."""


controller = ClaudeRuntimeController()
