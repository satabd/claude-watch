"""Runtime state machine binding Claude sessions to disposable Zellij panes.

Identity model: the Claude session id is the permanent identity. The Zellij
session (named ``cw-<first 8 of session id>``) and its pane are disposable
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
from . import zellij

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
# Modes the TUI's Shift+Tab cycle actually visits. The rest (auto, dont_ask,
# bypass) are launch-time `--permission-mode` choices — we can *display* them
# but cannot switch into them by cycling.
CYCLE_MODES = ("manual", "accept_edits", "plan")
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
            "external_pid": self.external_pid,
            "busy": self.busy,
            "awaiting_input": self.awaiting_input,
            "mode": self.mode,
            "mode_label": MODE_LABELS.get(self.mode or "", None),
            "working": self.working,
            "activity": self.activity,
            **({"detail": self.detail} if self.detail else {}),
        }


def parse_blocking_dialog(screen: str) -> dict | None:
    """Detect a claude TUI dialog awaiting a numbered choice.

    Conservative on purpose: only reports when the viewport tail shows at
    least two numbered options AND the "Esc to cancel" hint that claude
    renders under its dialogs. Continuation lines (wrapped option text with
    no number) are folded into the previous option's label. The question is
    the nearest non-option line above the first option, typically ending
    with "?" ("Do you want to create ac_signal.py?").
    """
    lines = screen.splitlines()[-25:]
    if not any("Esc to cancel" in ln for ln in lines):
        return None
    options: list[dict] = []
    question: str | None = None
    for i, ln in enumerate(lines):
        m = _DIALOG_OPTION_RE.match(ln)
        if m:
            options.append({"n": m.group(1), "label": m.group(2)})
            if question is None:
                # nearest meaningful line above the first option
                for prev in reversed(lines[:i]):
                    s = prev.strip()
                    if s and not s.startswith(("─", "╌", "│", "╭", "╰")):
                        question = s
                        break
        elif options and ln.startswith((" ", "\t")) and ln.strip():
            s = ln.strip()
            if "Esc to cancel" not in s and not s.startswith(("─", "╌")):
                options[-1]["label"] += " " + s
    if len(options) < 2:
        return None
    return {"question": question or "Claude is asking for input", "options": options}


def zellij_session_name(session_id: str) -> str:
    return f"cw-{session_id[:8]}"


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

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    # -- state ---------------------------------------------------------------

    async def get_state(
        self,
        session_id: str,
        jsonl_path: Path,
        *,
        remote_name: str | None = None,
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
        name = zellij_session_name(session_id)

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
                    working = bool(status.get("working"))
                    return RuntimeState(
                        state="managed",
                        controllable=True,
                        zellij_session=binding["zellij_session"],
                        pane_id=binding["pane_id"],
                        busy=working or busy,
                        awaiting_input=awaiting,
                        mode=status.get("mode"),
                        working=working,
                        activity=status.get("activity"),
                    )
            # Session or pane vanished — invalidate, fall through.
            _log.info("stale runtime binding for %s (zellij=%s)", session_id, state)
            db.runtime_binding_delete(session_id)
            return RuntimeState(state="resumable", controllable=True, busy=busy)

        # 2) Unbound but our named zellij session survives (watch restarted) —
        #    adopt it if it still has a claude pane.
        if await zellij.session_state(name) == "alive":
            panes = await zellij.list_panes(name)
            claude_panes = [
                p for p in panes if p[1] == "terminal" and "claude" in p[2].lower()
            ]
            if claude_panes:
                pane_id = claude_panes[0][0]
                db.runtime_binding_put(session_id, name, pane_id, cwd=None)
                _log.info("adopted surviving zellij session %s for %s", name, session_id)
                return RuntimeState(
                    state="managed",
                    controllable=True,
                    zellij_session=name,
                    pane_id=pane_id,
                    busy=busy,
                )

        # 3) External process?
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

        # 4) No identified process. If the session still looks mid-turn an
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
    ) -> RuntimeState:
        async with self._lock_for(session_id):
            state = await self.get_state(
                session_id, jsonl_path, remote_name=remote_name
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

            # inactive / resumable / (external now terminated) → build runtime
            name = zellij_session_name(session_id)
            await zellij.create_session(name)
            pane = await zellij.run_pane(
                name, cwd, ["claude", "--resume", session_id]
            )
            # The background-created session comes with a default shell pane
            # that just steals half the width from the claude TUI — drop it.
            try:
                for p_id, p_type, p_title in await zellij.list_panes(name):
                    if p_type == "terminal" and p_id != pane:
                        await zellij.close_pane(name, p_id)
            except zellij.ZellijError:
                pass  # cosmetic only
            # Don't hand the runtime out until the TUI is actually accepting
            # input — injecting into a booting claude loses the submit Enter.
            await self._wait_tui_ready(name, pane)
            db.runtime_binding_put(session_id, name, pane, cwd=cwd)
            _log.info(
                "managed runtime created: session=%s zellij=%s pane=%s",
                session_id, name, pane,
            )
            return RuntimeState(
                state="managed",
                controllable=True,
                zellij_session=name,
                pane_id=pane,
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
        if target not in CYCLE_MODES:
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

            for _ in range(5):  # >= one full lap over the known modes
                await zellij.write_bytes(name, pane, BACKTAB)
                await asyncio.sleep(0.35)
                now = parse_status(await zellij.dump_screen(name, pane))
                if now.get("mode") == target:
                    _log.info("mode set to %s for %s", target, session_id)
                    return target
            raise ControlRefused(
                f"could not reach {MODE_LABELS[target]} mode by cycling "
                "(this claude build may not offer it)"
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
