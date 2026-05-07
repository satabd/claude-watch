"""Codex (ChatGPT) provider — shells out to `codex exec`.

Uses the user's existing ChatGPT subscription via the Codex CLI's stored auth.

In codex-cli 0.128+ the structured response (with `--------` headers, `codex`
marker, `tokens used` footer) is written to STDERR, not stdout. STDOUT contains
the final answer text plus any windows-sandbox cleanup noise. We parse the
structured stderr first, fall back to stdout if needed.
"""
from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from typing import Final

CODEX_BIN: Final = shutil.which("codex") or "codex"
TIMEOUT_SECONDS = 120
REVIEW_TIMEOUT_SECONDS = 240  # reviews are larger; allow longer thinking


async def run(prompt: str, *, model: str | None = None) -> tuple[str, str]:
    args = [
        CODEX_BIN,
        "exec",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        # Lower reasoning effort = ~33% faster for translation/summary tasks.
        "-c",
        'model_reasoning_effort="low"',
        # Disable web search for predictable behavior on these prompts.
        "-c",
        'web_search="disabled"',
    ]
    if model:
        args += ["-m", model]
    args.append("-")  # read prompt from stdin

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")), timeout=TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise RuntimeError("codex CLI timed out")

    raw_out = stdout.decode("utf-8", errors="replace")
    raw_err = stderr.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        msg = (raw_err or raw_out).strip()
        # API model errors: extract the JSON detail
        m = re.search(r'"detail":\s*"([^"]+)"', msg)
        if m:
            detail = m.group(1)
            if "newer version of Codex" in detail or "not supported" in detail:
                raise RuntimeError(
                    f"Codex CLI rejected the model. Run `npm install -g @openai/codex` "
                    f"to upgrade, then try again. Original error: {detail}"
                )
            raise RuntimeError(f"Codex API: {detail}")
        raise RuntimeError(f"codex exec failed (exit {proc.returncode}): {msg[:500]}")

    # Try structured stderr first (codex 0.128+); fallback to stdout (older)
    answer = _extract_from_structured(raw_err)
    if not answer:
        answer = _extract_from_structured(raw_out)
    if not answer:
        # Last resort: clean stdout of windows-sandbox SUCCESS lines and return
        answer = _clean_stdout_only(raw_out)

    parsed_model = _model_from_metadata(raw_err) or model or "codex"
    return answer.strip(), parsed_model


# ---- parsers ----

# Section markers we want to drop if they appear in the body
_NOISE_LINES = {"thinking", "user", "codex", "tools", "tool"}


def _extract_from_structured(text: str) -> str:
    """Find the last `codex` header line and return the body up to `tokens used`."""
    if "codex" not in text:
        return ""
    lines = text.splitlines()
    last_codex = -1
    for i, line in enumerate(lines):
        if line.strip() == "codex":
            last_codex = i
    if last_codex == -1:
        return ""
    out: list[str] = []
    for line in lines[last_codex + 1 :]:
        s = line.rstrip()
        # End markers
        if s.startswith("tokens used") or s.startswith("session id:"):
            break
        # Drop noise section markers and SUCCESS lines from windows sandbox
        stripped = s.strip()
        if stripped in _NOISE_LINES:
            continue
        if stripped.startswith("SUCCESS: The process") or stripped.startswith("execution error"):
            continue
        out.append(s)
    return "\n".join(out).strip()


def _clean_stdout_only(text: str) -> str:
    """Strip windows-sandbox SUCCESS lines, return the rest."""
    keep: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("SUCCESS: The process") or s.startswith("execution error"):
            continue
        keep.append(line)
    return "\n".join(keep).strip()


_MODEL_RE = re.compile(r"^model:\s*(.+)$", re.MULTILINE)
_SESSION_ID_RE = re.compile(
    r"^session id:\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*$",
    re.MULTILINE,
)
# tokens used appears as a header line followed by the count on the next line:
#   tokens used
#   13,850
_TOKENS_USED_RE = re.compile(r"tokens used\s*\n\s*([\d,]+)", re.MULTILINE)


def _model_from_metadata(text: str) -> str | None:
    m = _MODEL_RE.search(text)
    if not m:
        return None
    return m.group(1).strip()


def parse_session_id(text: str) -> str | None:
    """Extract the ``session id: <UUID>`` line from codex stderr. Returns
    ``None`` if not found. Exposed for tests."""
    m = _SESSION_ID_RE.search(text)
    return m.group(1) if m else None


def parse_tokens_used(text: str) -> int | None:
    """Extract the integer following the ``tokens used`` header in codex
    stderr. Returns ``None`` if not found / parse fails."""
    m = _TOKENS_USED_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Review-mode entry point
# ---------------------------------------------------------------------------
#
# Used only by the Review Threads feature. Keeps Codex-specific facts here:
#   * session id is in stderr as ``session id: <UUID>``
#   * resume uses ``codex exec resume <UUID>`` (subcommand)
#   * resume does NOT accept ``-s`` — sandbox is inherited from the original
#     session; passing it errors out with "unexpected argument '-s'"
#   * resume's other flags are the same shape as cold start
#
# Other providers (Gemini, Claude self-review when added) will get their own
# typed result class. The generic ``run()`` function above is unchanged so
# translate / summary / prompt-writer keep working stateless-ly.


@dataclass(frozen=True)
class ReviewResult:
    text: str
    model: str
    session_id_out: str | None
    tokens_used: int | None


class CodexResumeFailed(Exception):
    """Raised when ``codex exec resume <UUID>`` fails specifically because
    the session id was rejected (or the subprocess otherwise errored under
    the resume code path). Caller should clear the stored session id and
    retry cold."""


def _build_review_args(
    session_id_in: str | None, model: str | None
) -> list[str]:
    args: list[str] = [CODEX_BIN, "exec"]
    if session_id_in:
        args.extend(["resume", session_id_in])
    args.append("--skip-git-repo-check")
    if not session_id_in:
        # Cold start sets sandbox; resume inherits and rejects -s.
        args.extend(["-s", "read-only"])
    args.extend(
        [
            "-c",
            'model_reasoning_effort="medium"',
            "-c",
            'web_search="disabled"',
        ]
    )
    if model:
        args.extend(["-m", model])
    args.append("-")  # read prompt from stdin
    return args


async def run_review(
    prompt: str,
    *,
    session_id_in: str | None = None,
    model: str | None = None,
    timeout: float = REVIEW_TIMEOUT_SECONDS,
) -> ReviewResult:
    """Run a review-mode codex call.

    If ``session_id_in`` is given, attempts ``codex exec resume <UUID>``. On
    *any* non-zero exit under the resume path we raise
    :class:`CodexResumeFailed` so the caller can fall back to a cold call.
    Cold calls that fail raise :class:`RuntimeError` with the codex error
    detail (matching the existing ``run()`` behavior).
    """
    args = _build_review_args(session_id_in, model)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")), timeout=timeout
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        if session_id_in:
            raise CodexResumeFailed("codex resume timed out")
        raise RuntimeError("codex CLI timed out")

    raw_out = stdout.decode("utf-8", errors="replace")
    raw_err = stderr.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        msg = (raw_err or raw_out).strip()[:1000]
        if session_id_in:
            raise CodexResumeFailed(
                f"codex resume failed (exit {proc.returncode}): {msg}"
            )
        # Mirror run() error mapping for cold starts.
        m = re.search(r'"detail":\s*"([^"]+)"', msg)
        if m:
            raise RuntimeError(f"Codex API: {m.group(1)}")
        raise RuntimeError(f"codex exec failed (exit {proc.returncode}): {msg}")

    answer = _extract_from_structured(raw_err)
    if not answer:
        answer = _extract_from_structured(raw_out)
    if not answer:
        answer = _clean_stdout_only(raw_out)

    return ReviewResult(
        text=answer.strip(),
        model=_model_from_metadata(raw_err) or model or "codex",
        session_id_out=parse_session_id(raw_err),
        tokens_used=parse_tokens_used(raw_err),
    )
