"""Thin async wrapper around the Zellij CLI.

Every quirk in here was verified empirically against zellij 0.44.3:

* Targeting an existing session from outside uses the ``ZELLIJ_SESSION_NAME``
  environment variable. BUT: if the named session does not exist and exactly
  one other session is running, zellij **silently falls back to it** — and
  with several sessions running it **blocks forever**. Both are catastrophic
  for prompt injection (text lands in an unrelated terminal). Therefore every
  targeted call (a) verifies the session exists via ``list-sessions`` first
  and (b) runs under a hard timeout.

* ``zellij attach <name> --create-background`` creates a detached session —
  but panics if ``ZELLIJ_SESSION_NAME`` in the environment already names the
  target. We always scrub the variable and set it explicitly per call.

* ``zellij run`` prints the created pane id (``terminal_<n>``) on stdout.

* ``action write-chars`` passes text as a single argv element — no shell —
  so quotes, backticks, ``$()``, Arabic and CJK arrive byte-perfect.
  Newlines would submit line-by-line in a TUI, so multiline text is wrapped
  in bracketed-paste escape sequences (ESC[200~ ... ESC[201~) via
  ``action write`` byte lists; Claude Code's composer treats that as a paste
  and keeps the newlines without submitting.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from ..providers._bin import ProviderBinaryNotFound, resolve_bin

_log = logging.getLogger("watcher.runtime.zellij")

CALL_TIMEOUT_S = 5.0
# ESC [ 2 0 0 ~ / ESC [ 2 0 1 ~
_PASTE_START = [27, 91, 50, 48, 48, 126]
_PASTE_END = [27, 91, 50, 48, 49, 126]
ENTER = [13]
ESC = [27]


class ZellijError(RuntimeError):
    """A zellij invocation failed, timed out, or targeted a dead session."""


class ZellijUnavailable(ZellijError):
    """The zellij binary is not installed / not supported on this platform."""


@dataclass
class ZellijSession:
    name: str
    exited: bool


def zellij_bin() -> str:
    try:
        return resolve_bin("zellij")
    except ProviderBinaryNotFound as e:
        raise ZellijUnavailable(str(e)) from e


def _env_for(session: str | None) -> dict[str, str]:
    """Process env scrubbed for spawning zellij, optionally targeting a session.

    ZELLIJ_*: a stale/self-referencing session var breaks create/attach.

    CLAUDECODE / CLAUDE_*: if claude-watch itself was started from inside a
    Claude Code session (nested shell, agent, `start.sh` from a claude pane),
    these leak through the zellij server into every pane it spawns — and a
    resumed `claude` TUI that sees CLAUDE_CODE_CHILD_SESSION=1 silently
    DISABLES transcript saving ("inherited CLAUDE_CODE env"), which severs
    the entire read path of this app. Found the hard way. ANTHROPIC_* is
    kept: base-URL/auth overrides are deliberate user config.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not (
            k.startswith("ZELLIJ")
            or k.startswith("CLAUDE_")
            or k == "CLAUDECODE"
        )
    }
    if session is not None:
        env["ZELLIJ_SESSION_NAME"] = session
    return env


async def _run(
    args: list[str],
    *,
    session: str | None = None,
    timeout: float = CALL_TIMEOUT_S,
) -> str:
    proc = await asyncio.create_subprocess_exec(
        zellij_bin(),
        *args,
        env=_env_for(session),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise ZellijError(
            f"zellij {' '.join(args[:2])} timed out after {timeout}s "
            f"(session={session!r}) — likely a dead/ambiguous session target"
        )
    if proc.returncode != 0:
        raise ZellijError(
            f"zellij {' '.join(args[:2])} failed (rc={proc.returncode}): "
            f"{stderr.decode(errors='replace').strip()[:300]}"
        )
    return stdout.decode(errors="replace")


def parse_sessions(raw: str) -> list[ZellijSession]:
    """Parse ``zellij list-sessions -n`` output lines."""
    out: list[ZellijSession] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        name = line.split()[0]
        out.append(ZellijSession(name=name, exited="(EXITED" in line))
    return out


async def list_sessions() -> list[ZellijSession]:
    # rc=1 with empty output when no sessions exist at all
    try:
        raw = await _run(["list-sessions", "-n"])
    except ZellijError as e:
        if "No" in str(e) or "rc=1" in str(e):
            return []
        raise
    return parse_sessions(raw)


async def session_state(name: str) -> str:
    """'alive' | 'exited' | 'missing' — the precheck used before every action."""
    for s in await list_sessions():
        if s.name == name:
            return "exited" if s.exited else "alive"
    return "missing"


async def _require_alive(name: str) -> None:
    state = await session_state(name)
    if state != "alive":
        raise ZellijError(f"zellij session {name!r} is {state}; refusing to target it")


async def create_session(name: str) -> None:
    """Create a detached session, clearing any EXITED remnant of the name."""
    state = await session_state(name)
    if state == "alive":
        return
    if state == "exited":
        # A resurrected session would replay its old commands; we want a
        # clean slate and will run `claude --resume` ourselves.
        await _run(["delete-session", name])
    await _run(["attach", name, "--create-background"], timeout=10.0)
    if await session_state(name) != "alive":
        raise ZellijError(f"failed to create zellij session {name!r}")


async def run_pane(name: str, cwd: str | None, command: list[str]) -> str:
    """Run a command in a new pane of session `name`; returns the pane id."""
    await _require_alive(name)
    args = ["run"]
    if cwd:
        args += ["--cwd", cwd]
    args += ["--", *command]
    out = (await _run(args, session=name, timeout=10.0)).strip()
    # stdout is the pane id, e.g. "terminal_1"
    pane = out.splitlines()[-1].strip() if out else ""
    if not pane.startswith(("terminal_", "plugin_")):
        raise ZellijError(f"unexpected `zellij run` output: {out[:120]!r}")
    return pane


async def list_panes(name: str) -> list[tuple[str, str, str]]:
    """[(pane_id, type, title)] for session `name`."""
    await _require_alive(name)
    raw = await _run(["action", "list-panes"], session=name)
    rows: list[tuple[str, str, str]] = []
    for line in raw.splitlines()[1:]:  # skip header
        parts = line.split(None, 2)
        if len(parts) >= 2:
            rows.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    return rows


async def write_bytes(name: str, pane: str, byte_list: list[int]) -> None:
    await _require_alive(name)
    await _run(
        ["action", "write", "-p", pane, *[str(b) for b in byte_list]],
        session=name,
    )


async def write_chars(name: str, pane: str, text: str) -> None:
    await _require_alive(name)
    await _run(["action", "write-chars", "-p", pane, text], session=name)


def paste_chunks(text: str) -> list[tuple[str, list[int] | str]]:
    """Build the exact write sequence for injecting `text` WITHOUT submitting.

    Multiline text is wrapped in bracketed paste so the TUI inserts newlines
    instead of submitting on each one. Returned as a list of
    ("bytes", [...]) / ("chars", "...") steps — separated out for testability.
    """
    steps: list[tuple[str, list[int] | str]] = []
    if "\n" in text or "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        steps.append(("bytes", _PASTE_START))
        steps.append(("chars", text))
        steps.append(("bytes", _PASTE_END))
    else:
        steps.append(("chars", text))
    return steps


async def type_text(name: str, pane: str, text: str) -> None:
    """Inject text into the pane's composer without submitting it."""
    for kind, payload in paste_chunks(text):
        if kind == "bytes":
            await write_bytes(name, pane, payload)  # type: ignore[arg-type]
        else:
            await write_chars(name, pane, payload)  # type: ignore[arg-type]


async def submit(name: str, pane: str) -> None:
    await write_bytes(name, pane, ENTER)


async def send_escape(name: str, pane: str) -> None:
    await write_bytes(name, pane, ESC)


async def dump_screen(name: str, pane: str) -> str:
    """Rendered viewport text of a pane (used for TUI readiness checks)."""
    await _require_alive(name)
    return await _run(["action", "dump-screen", "-p", pane], session=name)


async def close_pane(name: str, pane: str) -> None:
    await _require_alive(name)
    await _run(["action", "close-pane", "-p", pane], session=name)


async def kill_session(name: str) -> None:
    if await session_state(name) == "alive":
        await _run(["kill-session", name])


async def delete_session(name: str) -> None:
    if await session_state(name) != "missing":
        try:
            await _run(["kill-session", name])
        except ZellijError:
            pass
        await _run(["delete-session", name, "--force"])
