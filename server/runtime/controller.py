"""Runtime state machine binding Claude sessions to disposable Zellij panes.

Identity model: the Claude session id is the permanent identity. The Zellij
session (named after the *project*, e.g. ``rumailahub``) and the tab/pane
holding this particular conversation (``rumailahub-<label>``) are disposable
runtime bindings persisted in SQLite and re-verified on every use — a binding
is *never* trusted without checking the Zellij session is actually alive.
Recovery from pane closure / zellij death / machine restart therefore falls
out naturally: verification fails, the binding is invalidated, the state
becomes ``resumable`` and the next control request rebuilds the runtime.

Safety rules for taking over an *external* claude process (one we did not
start):

1. The process must be positively identified: its argv must contain
   ``--resume <session-id>`` (or ``--resume=<id>``) for the exact session.
   Fresh TUI sessions without ``--resume`` cannot be matched with certainty
   and are monitor-only.
2. Embedded / headless claudes are NEVER touched: anything with
   ``--output-format``, ``-p``/``--print``, or living inside ``Claude.app``
   (Claude Desktop's agent runtime) is not an interactive terminal — killing
   it would sever someone else's conversation, not free a TTY.
3. The session must look idle BOTH by JSONL mtime age and by transcript
   shape (no user turn or tool_use awaiting a response).
4. The caller must pass ``allow_takeover=True`` — the UI gets it from an
   explicit user confirmation, never a default.
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
        name = project_name(cwd)
        want_title = pane_title(session_id, cwd, title)

        # 1) Existing binding — verify, never trust.
        binding = db.runtime_binding_get(session_id)
        if binding:
            state = await zellij.session_state(binding["zellij_session"])
            if state == "alive":
                panes = await zellij.list_panes(binding["zellij_session"])
                pane_ids = {p[0] for p in panes}
                if binding["pane_id"] in pane_ids:
                    db.runtime_binding_touch(session_id)
                    awaiting = None
                    status: dict = {}
                    try:
                        screen = await zellij.dump_screen(
                            binding["zellij_session"], binding["pane_id"]
                        )
                        awaiting = parse_blocking_dialog(screen)
                        status = parse_status(screen)
                    except zellij.ZellijError:
                        pass  # screen introspection is best-effort
                    # The live pane is a better "is it working" signal than
                    # JSONL mtime: it reacts instantly and never lags.
                    # A pane blocked on a dialog often *keeps* its spinner and
                    # "esc to interrupt" hint on screen. Reporting that as
                    # "working" is what made a session asking a question look
                    # permanently stuck, so the question always wins.
                    working = bool(status.get("working")) and awaiting is None
                    return RuntimeState(
                        state="managed",
                        controllable=True,
                        zellij_session=binding["zellij_session"],
                        pane_id=binding["pane_id"],
                        pane_title=next(
                            (p[2] for p in panes if p[0] == binding["pane_id"]), None
                        ),
                        zellij_tab=binding["tab_name"],
                        busy=(working or busy) and awaiting is None,
                        awaiting_input=awaiting,
                        mode=status.get("mode"),
                        working=working,
                        activity=status.get("activity") if working else None,
                    )
            # Session or pane vanished — invalidate, fall through.
            _log.info("stale runtime binding for %s (zellij=%s)", session_id, state)
            db.runtime_binding_delete(session_id)
            return RuntimeState(state="resumable", controllable=True, busy=busy)

        # 2) Unbound but a zellij session of ours survives (watch restarted) —
        #    adopt the pane that carries this session's title. The project
        #    session holds one pane per claude session, so matching on the
        #    exact `<project>-<session>` title is what keeps them apart; the
        #    legacy per-session `cw-<id8>` layout had only one pane, so any
        #    claude-looking pane in it is unambiguous.
        for candidate, exact in ((name, True), (legacy_session_name(session_id), False)):
            if await zellij.session_state(candidate) != "alive":
                continue
            panes = await zellij.list_panes(candidate)
            match = [
                p
                for p in panes
                if p[1] == "terminal"
                and (p[2] == want_title if exact else "claude" in p[2].lower())
            ]
            if match:
                pane_id = match[0][0]
                db.runtime_binding_put(
                    session_id, candidate, pane_id, cwd=cwd, tab_name=match[0][2]
                )
                _log.info(
                    "adopted surviving zellij pane %s/%s for %s",
                    candidate, pane_id, session_id,
                )
                return RuntimeState(
                    state="managed",
                    controllable=True,
                    zellij_session=candidate,
                    pane_id=pane_id,
                    pane_title=match[0][2],
                    zellij_tab=match[0][2],
                    busy=busy,
                )

        # 3) Does a live claude already own this session? Claude Code's own
        #    registry answers this exactly, including for a plain `claude`
        #    started with no session flag — the case the argv scan below is
        #    blind to, and the one that used to let us start a *second*
        #    claude on the same transcript.
        owners = registry.owners_of(session_id)
        if owners:
            owner = owners[-1]
            if len(owners) > 1:
                # Already broken: several claudes are appending to one
                # transcript. Say so — the pane will look like it is ignoring
                # the conversation, and no amount of staring at it explains why.
                pids = ", ".join(str(o.pid) for o in owners)
                _log.warning(
                    "session %s has %d live claude owners (pids %s)",
                    session_id, len(owners), pids,
                )
                return RuntimeState(
                    state="external_busy",
                    controllable=False,
                    external_pid=owner.pid,
                    busy=True,
                    reason=f"{len(owners)} claude processes (pids {pids}) are "
                    "driving this session at once, so their writes interleave "
                    "and neither view is complete. Close all but one, then "
                    "reload.",
                    detail={"owner_pids": [o.pid for o in owners]},
                )
            # `status` is written on transitions, not on a timer, so it goes
            # unknown on any session that has been sitting still. Fall back to
            # the JSONL heuristic there. Either way the session is now
            # `external_*` rather than `inactive`, so control needs an explicit
            # takeover — which is the property that actually prevents a
            # second claude on the same transcript.
            owner_busy = owner.busy
            effective_busy = busy if owner_busy is None else owner_busy
            if not owner.takeoverable:
                return RuntimeState(
                    state="external_busy" if effective_busy else "external_idle",
                    controllable=False,
                    external_pid=owner.pid,
                    busy=effective_busy,
                    reason=f"Session is driven by {owner.describe()}, which is "
                    "not a terminal claude-watch can take over; monitor-only.",
                )
            if effective_busy:
                return RuntimeState(
                    state="external_busy",
                    controllable=False,
                    external_pid=owner.pid,
                    busy=True,
                    reason=f"{owner.describe()} is working on this session; "
                    "takeover is blocked until it goes idle.",
                )
            return RuntimeState(
                state="external_idle",
                controllable=True,
                external_pid=owner.pid,
                busy=False,
                reason=f"{owner.describe()} currently owns this session. "
                "Taking control closes it and resumes the session under "
                "claude-watch — otherwise two claudes would write to the "
                "same transcript.",
            )

        # 4) Fallback for claude builds with no session registry: identify by
        #    argv. Only catches processes launched with --resume/--session-id.
        for proc in find_claude_processes():
            if proc.resume_id and session_id.startswith(proc.resume_id):
                if proc.embedded:
                    return RuntimeState(
                        state="external_busy" if busy else "external_idle",
                        controllable=False,
                        external_pid=proc.pid,
                        busy=busy,
                        reason="Session is driven by an embedded runtime "
                        "(Claude Desktop / SDK); monitor-only.",
                    )
                if busy:
                    return RuntimeState(
                        state="external_busy",
                        controllable=False,
                        external_pid=proc.pid,
                        busy=True,
                        reason="Claude is working; takeover is blocked until idle.",
                    )
                return RuntimeState(
                    state="external_idle",
                    controllable=True,
                    external_pid=proc.pid,
                    busy=False,
                    reason="Taking control will close the external claude TUI "
                    "and resume the session under claude-watch.",
                )

        # 5) Nothing *names* this session, but an unregistered claude may
        #    still be driving it. One that inherited CLAUDE_* env writes no
        #    registry record, and started as a plain `claude` it has no argv
        #    flag either — invisible to both checks above, which is exactly
        #    how a second claude once got spawned onto this transcript. Only
        #    its directory is knowable, so a match there means "unknown", not
        #    "free": refuse rather than resume.
        #
        #    Not offered as a takeover: we cannot tell which session that
        #    process holds, so terminating it could close a different one.
        if unknown := registry.unidentified_claudes(cwd):
            pids = ", ".join(str(u.pid) for u in unknown)
            _log.info(
                "session %s: %d unregistered claude(s) in %s (pids %s)",
                session_id, len(unknown), cwd, pids,
            )
            return RuntimeState(
                state="external_idle",
                controllable=False,
                external_pid=unknown[0].pid,
                busy=busy,
                reason=(
                    f"An unregistered claude (pid {pids}) is running in this "
                    "project. It publishes no session id, so claude-watch "
                    "cannot tell whether it is driving this session — and "
                    "resuming anyway would put two claudes on one transcript. "
                    "Close it (or start it in its own terminal) and reload."
                ),
                detail={"unregistered_pids": [u.pid for u in unknown]},
            )

        # 6) No identified process. If the session still looks mid-turn an
        #    unidentifiable claude may own it — stay hands-off, but report
        #    which signal tripped so the UI/user can tell why.
        if busy:
            try:
                age = time.time() - jsonl_path.stat().st_mtime
            except OSError:
                age = -1.0
            recently_written = 0 <= age < IDLE_AFTER_S
            return RuntimeState(
                state="external_busy",
                controllable=False,
                busy=True,
                reason=(
                    "Transcript was written moments ago but no owning claude "
                    "process could be identified; monitor-only."
                    if recently_written
                    else "Transcript ends mid-turn (awaiting a response or "
                    "tool result) and no owning claude process could be "
                    "identified; monitor-only."
                ),
                detail={"mtime_age_s": round(age, 1)},
            )
        return RuntimeState(state="inactive", controllable=True, busy=False)

    # -- control -------------------------------------------------------------

    async def ensure_managed(
        self,
        session_id: str,
        jsonl_path: Path,
        cwd: str | None,
        *,
        allow_takeover: bool = False,
        remote_name: str | None = None,
        title: str | None = None,
    ) -> RuntimeState:
        async with self._lock_for(session_id):
            state = await self.get_state(
                session_id, jsonl_path, remote_name=remote_name, cwd=cwd, title=title
            )
            if state.state == "managed":
                return state
            if not state.controllable:
                raise ControlRefused(state.reason or f"state={state.state}")

            if state.state == "external_idle":
                if not allow_takeover:
                    raise TakeoverConfirmationRequired(
                        state.reason or "external claude TUI must be closed first"
                    )
                assert state.external_pid is not None
                await self._terminate_pid(state.external_pid)
                # Re-verify nothing raced us back to busy.
                if session_is_busy(jsonl_path):
                    raise ControlRefused(
                        "session became active again during takeover; aborted"
                    )

            # Last-mile guard. get_state() ran before any awaits above, and a
            # `claude` can be started by hand in the seconds since. Spawning a
            # second one is the single most damaging thing this class can do —
            # two interactive processes appending to one JSONL — so re-ask the
            # registry immediately before committing.
            if survivors := registry.owners_of(session_id):
                raise ControlRefused(
                    f"{survivors[-1].describe()} owns this session; refusing to "
                    "start a second claude on the same transcript"
                )
            if unknown := registry.unidentified_claudes(cwd):
                raise ControlRefused(
                    f"{unknown[0].describe()} is running in this project and "
                    "publishes no session id, so it may be driving this "
                    "session; refusing to start a second claude on the same "
                    "transcript"
                )

            # inactive / resumable / (external now terminated) → build runtime.
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
            db.runtime_binding_put(session_id, name, pane, cwd=cwd, tab_name=tab)
            _log.info(
                "managed runtime created: session=%s zellij=%s tab=%s pane=%s",
                session_id, name, tab, pane,
            )
            return RuntimeState(
                state="managed",
                controllable=True,
                zellij_session=name,
                pane_id=pane,
                pane_title=tab,
                zellij_tab=tab,
                mode=DEFAULT_PERMISSION_MODE,
                busy=False,
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

    async def _terminate_pid(self, pid: int) -> None:
        """SIGTERM exactly one positively-identified claude TUI; wait for exit."""
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
            f"external claude (pid {pid}) did not exit within "
            f"{TERMINATE_WAIT_S:.0f}s; takeover aborted (not killed with -9 "
            "on purpose — it may be mid-write)"
        )

    # -- prompt delivery -----------------------------------------------------

    async def send_prompt(self, session_id: str, prompt: str) -> None:
        """Inject prompt text and submit it. Caller must be in managed state."""
        async with self._lock_for(session_id):
            binding = db.runtime_binding_get(session_id)
            if not binding:
                raise ControlRefused("no managed runtime; take control first")
            name, pane = binding["zellij_session"], binding["pane_id"]
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

        So this verifies against the registry and reaps a survivor with
        SIGTERM. Never SIGKILL: a claude caught mid-write to its JSONL should
        be allowed to finish the line.
        """
        async with self._lock_for(session_id):
            binding = db.runtime_binding_get(session_id)
            if not binding:
                return {"released": False, "reason": "no managed runtime"}
            name, pane = binding["zellij_session"], binding["pane_id"]

            if await zellij.session_state(name) == "alive":
                try:
                    await zellij.close_pane(name, pane)
                except zellij.ZellijError as e:
                    _log.info("close_pane failed during release of %s: %s", session_id, e)

            # Give the TUI a moment to exit on its own before reaching for a
            # signal — a clean exit flushes; a signal races the flush.
            reaped: list[int] = []
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                if not registry.owners_of(session_id):
                    break
                await asyncio.sleep(0.5)
            for survivor in registry.owners_of(session_id):
                _log.warning(
                    "claude %s survived pane close for %s; terminating",
                    survivor.pid, session_id,
                )
                try:
                    await self._terminate_pid(survivor.pid)
                    reaped.append(survivor.pid)
                except ControlRefused as e:
                    _log.error("could not reap %s: %s", survivor.pid, e)

            db.runtime_binding_delete(session_id)
            still = [o.pid for o in registry.owners_of(session_id)]
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


class TakeoverConfirmationRequired(ControlRefused):
    """Taking over an external TUI needs an explicit user confirmation."""


controller = ClaudeRuntimeController()
