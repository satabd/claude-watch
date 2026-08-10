"""Summarize a session *from inside that session*.

The original implementation rebuilt a condensed transcript in the browser,
posted it up, and pasted it into a brand-new `claude -p` call. That re-sends
the whole conversation as fresh input on every summary — expensive — and the
model only ever sees the truncated digest, not the real turns, so it happily
invents detail the digest dropped.

Asking the session itself is strictly better on both counts: the conversation
is already in its context (and, for a live pane, already in the prompt cache),
and nothing is truncated.

Two delivery paths, picked by what is actually running:

``pane``
    The session has a managed Zellij pane. The prompt is typed into it like
    any other prompt and the answer is read back out of the transcript. The
    summary becomes a real turn in the conversation — visible in the timeline,
    and in the pane if you attach to it.

``resume``
    Nothing is running. ``claude --resume <id> --print`` replays the session
    headlessly and prints the answer. Never used while a pane owns the
    session: two claudes appending to one JSONL interleave their writes.

Both paths fall back to the caller's pasted transcript when they fail, so a
summary is always produced.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from . import db
from .providers._bin import ProviderBinaryNotFound, resolve_bin
from .runtime import zellij
from .runtime.controller import ControlRefused, controller, find_claude_processes

_log = logging.getLogger("watcher.session_summary")

# Long enough for a real summarizing turn on a big session, short enough that
# a wedged pane doesn't hold the HTTP request open forever.
ANSWER_TIMEOUT_S = 240.0
RESUME_TIMEOUT_S = 240.0
POLL_INTERVAL_S = 1.0

SUMMARY_PROMPT = (
    "Summarize this session for me — you already have the whole conversation, "
    "so work from it directly and do not re-read the transcript from disk.\n\n"
    "Reply with markdown only, no preamble and no closing remarks:\n"
    "- **Goal** (1 sentence): what I was trying to accomplish.\n"
    "- **What was built / changed** (bullets): concrete artifacts, files "
    "modified, decisions taken.\n"
    "- **Open threads** (bullets, omit if none): unfinished work, follow-ups, "
    "known issues.\n\n"
    "Keep it under 300 words. Do not invent anything that did not happen. "
    "Do not use any tools — just answer."
)


class SummaryUnavailable(RuntimeError):
    """Neither in-session path could produce a summary."""


# ---------------------------------------------------------------------------
# Reading the answer back out of the transcript
# ---------------------------------------------------------------------------

def _assistant_texts(path: Path, from_offset: int) -> list[str]:
    """Assistant text blocks written to `path` at or after `from_offset`."""
    out: list[str] = []
    try:
        with path.open("rb") as f:
            f.seek(from_offset)
            chunk = f.read()
    except OSError:
        return out
    for line in chunk.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # a partially-flushed final line; it'll be whole next poll
        if obj.get("type") != "assistant":
            continue
        content = (obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                if text := (blk.get("text") or "").strip():
                    out.append(text)
    return out


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


async def _await_answer(path: Path, from_offset: int) -> str:
    """Wait for the pane's reply to land in the transcript.

    The turn is done when the transcript stops growing *and* at least one
    assistant text block has appeared. Watching for quiet rather than for a
    single block matters because a summary can arrive as several blocks (or
    after a stray tool call), and grabbing the first one would truncate it.
    """
    deadline = time.monotonic() + ANSWER_TIMEOUT_S
    last_size = _size(path)
    stable_since: float | None = None
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL_S)
        size = _size(path)
        if size != last_size:
            last_size = size
            stable_since = None
            continue
        texts = _assistant_texts(path, from_offset)
        if not texts:
            continue
        now = time.monotonic()
        if stable_since is None:
            stable_since = now
        elif now - stable_since >= 2.0:
            return "\n\n".join(texts).strip()
    texts = _assistant_texts(path, from_offset)
    if texts:
        return "\n\n".join(texts).strip()
    raise SummaryUnavailable(
        f"the session did not answer within {ANSWER_TIMEOUT_S:.0f}s"
    )


# ---------------------------------------------------------------------------
# Delivery paths
# ---------------------------------------------------------------------------

async def _via_pane(session_id: str, path: Path) -> str:
    binding = db.runtime_binding_get(session_id)
    if not binding:
        raise SummaryUnavailable("no managed pane")
    if await zellij.session_state(binding["zellij_session"]) != "alive":
        raise SummaryUnavailable("managed zellij session is gone")
    offset = _size(path)
    await controller.send_prompt(session_id, SUMMARY_PROMPT)
    return await _await_answer(path, offset)


async def _via_resume(session_id: str, cwd: str | None) -> str:
    # Same rule as the pane path, for the same reason: a second claude
    # appending to a JSONL another claude is already writing interleaves the
    # two, and the corruption is not recoverable.
    for proc_info in find_claude_processes():
        if proc_info.resume_id and session_id.startswith(proc_info.resume_id):
            raise SummaryUnavailable(
                f"claude (pid {proc_info.pid}) already owns this session"
            )
    proc = await asyncio.create_subprocess_exec(
        resolve_bin("claude"),
        "--resume",
        session_id,
        "--print",
        SUMMARY_PROMPT,
        cwd=cwd or None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=RESUME_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise SummaryUnavailable(f"claude --resume timed out after {RESUME_TIMEOUT_S:.0f}s")
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise SummaryUnavailable(f"claude --resume failed: {err[:300]}")
    out = stdout.decode("utf-8", errors="replace").strip()
    if not out:
        raise SummaryUnavailable("claude --resume produced no output")
    return out


async def summarize_in_session(
    session_id: str, path: Path, cwd: str | None
) -> tuple[str, str]:
    """Summarize `session_id` from its own context. Returns (summary, source).

    `source` is "pane" or "resume" — the caller surfaces it so the user can
    tell where the answer came from (and, for "pane", why a new turn just
    appeared in their timeline).
    """
    try:
        zellij.zellij_bin()
        has_zellij = True
    except zellij.ZellijUnavailable:
        has_zellij = False

    if has_zellij:
        try:
            return await _via_pane(session_id, path), "pane"
        except (SummaryUnavailable, ControlRefused, zellij.ZellijError) as e:
            _log.info("pane summary unavailable for %s: %s", session_id, e)

    try:
        return await _via_resume(session_id, cwd), "resume"
    except ProviderBinaryNotFound as e:
        raise SummaryUnavailable(str(e))
