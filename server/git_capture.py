"""Read-only git status / diff capture for the Review Threads feature.

This module is *strictly* read-only. Every command is invoked via
``asyncio.create_subprocess_exec`` (no shell, no string interpolation) and
the program name is the literal string ``"git"`` followed by an arg
allowlist. Destructive verbs (``add``, ``commit``, ``reset``, ``checkout``,
``clean``, ``restore``, ``push``, ``rebase``, ``stash``) are never used.
The cwd is the project's resolved working directory; we never expand a
user-supplied path.

Output is bounded:
  * Diffs over MAX_DIFF_BYTES are head-truncated and a marker is appended.
  * The list of dirty files is unbounded in the captured object, but the
    Review Packet builder slices it before sending.
  * Each subprocess has a 5 s timeout; on timeout we kill the process.

Returned :class:`GitCapture` is a frozen dataclass so callers cannot
accidentally mutate captured state when building a packet.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("watcher.git")

GIT_TIMEOUT_S = 5.0
MAX_DIFF_BYTES = 60_000


@dataclass(frozen=True)
class DirtyEntry:
    path: str
    status: str  # short status code, e.g. "M.", ".M", "??"


@dataclass(frozen=True)
class GitCapture:
    is_repo: bool
    branch: str | None = None
    ahead: int = 0
    behind: int = 0
    dirty: tuple[DirtyEntry, ...] = ()
    diff: str = ""
    diff_truncated: bool = False
    diff_byte_count: int = 0
    error: str | None = None


_NULL_CAPTURE = GitCapture(is_repo=False)


async def _git(
    cwd: Path, *args: str, timeout: float = GIT_TIMEOUT_S
) -> tuple[int, str, str]:
    """Invoke ``git -C <cwd> <args>`` with no shell. Returns (rc, stdout, stderr).

    rc == -1 indicates the process could not be started (e.g. ``git`` not on
    PATH) or timed out. Errors are logged at debug level — most callers
    treat any failure as "no data" and fall back gracefully.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(cwd),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        _log.debug("git binary not found on PATH")
        return -1, "", "git not found"
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        _log.debug("git %s timed out after %.1fs", " ".join(args[:2]), timeout)
        return -1, "", "timeout"
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _parse_status_v2(out: str) -> tuple[str | None, int, int, list[DirtyEntry]]:
    """Parse ``git status --porcelain=v2 --branch`` output.

    Format reference: https://git-scm.com/docs/git-status#_porcelain_format_version_2
    """
    branch: str | None = None
    ahead = 0
    behind = 0
    dirty: list[DirtyEntry] = []
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head ") :].strip()
            if branch == "(detached)":
                branch = None
        elif line.startswith("# branch.ab "):
            # "# branch.ab +1 -2"
            parts = line.split()
            if len(parts) >= 4:
                try:
                    ahead = int(parts[2].lstrip("+"))
                    behind = int(parts[3].lstrip("-"))
                except ValueError:
                    pass
        elif line.startswith("1 ") or line.startswith("2 "):
            # "1 .M N... <100644> <100644> <100644> <hashIndex> <hashWorktree> <path>"
            # Path is the 9th whitespace-separated field for type "1".
            # For type "2" (renames) the path field is "<sep><orig>" with sep \t,
            # but a simple split-on-space is enough to extract status XY at
            # position 1.
            parts = line.split(" ", 8)
            if len(parts) >= 9:
                status = parts[1]
                path = parts[8]
                # For renames the path is "new\torig"; keep the new name.
                if "\t" in path:
                    path = path.split("\t", 1)[0]
                dirty.append(DirtyEntry(path=path, status=status))
        elif line.startswith("? "):
            dirty.append(DirtyEntry(path=line[2:].strip(), status="??"))
    return branch, ahead, behind, dirty


async def capture(cwd: Path | str | None) -> GitCapture:
    """Run a bounded read-only git capture of ``cwd``.

    Returns a non-repo :class:`GitCapture` when:
      * cwd is None or doesn't exist on disk
      * cwd is not a git working tree
      * git is not installed
      * any subprocess times out

    The function never raises — failure modes are encoded in the returned
    dataclass (``is_repo=False``, ``error`` set when meaningful)."""
    if cwd is None:
        return _NULL_CAPTURE
    cwd_path = Path(cwd)
    if not cwd_path.exists():
        return GitCapture(is_repo=False, error="cwd does not exist")

    rc, _, _ = await _git(cwd_path, "rev-parse", "--show-toplevel")
    if rc != 0:
        # Not a repo, or git missing — both mean "no data, no error".
        return _NULL_CAPTURE

    rc_status, status_out, _ = await _git(
        cwd_path, "status", "--porcelain=v2", "--branch"
    )
    if rc_status == 0:
        branch, ahead, behind, dirty = _parse_status_v2(status_out)
    else:
        branch, ahead, behind, dirty = None, 0, 0, []

    rc_diff, diff, _ = await _git(cwd_path, "diff", "--no-color")
    if rc_diff != 0 or not diff.strip():
        # If working-tree diff is empty, fall back to staged diff so a "git
        # add"-already workflow still has something to review.
        rc_cached, diff_cached, _ = await _git(
            cwd_path, "diff", "--no-color", "--cached"
        )
        if rc_cached == 0 and diff_cached.strip():
            diff = diff_cached

    diff_byte_count = len(diff.encode("utf-8"))
    diff_truncated = False
    if diff_byte_count > MAX_DIFF_BYTES:
        truncated = diff.encode("utf-8")[:MAX_DIFF_BYTES].decode(
            "utf-8", errors="ignore"
        )
        diff = (
            truncated
            + f"\n\n... [truncated; original diff was {diff_byte_count} bytes; "
            f"showing first {MAX_DIFF_BYTES}]"
        )
        diff_truncated = True

    return GitCapture(
        is_repo=True,
        branch=branch,
        ahead=ahead,
        behind=behind,
        dirty=tuple(dirty),
        diff=diff,
        diff_truncated=diff_truncated,
        diff_byte_count=diff_byte_count,
    )
