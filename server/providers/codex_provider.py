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
from dataclasses import dataclass

from ._bin import resolve_bin

TIMEOUT_SECONDS = 120
REVIEW_TIMEOUT_SECONDS = 240  # reviews are larger; allow longer thinking


# Lines codex emits that are noise, not the failure. The models-cache line in
# particular is logged at ERROR level but is non-fatal — surfacing it as *the*
# error sends people chasing a cache bug when they have actually run out of
# quota.
_NOISE_PATTERNS = (
    "codex_models_manager::cache",
    "failed to load models cache",
)


def _explain_failure(returncode: int, raw_err: str, raw_out: str) -> str:
    """Turn a failed `codex exec` into one actionable sentence.

    codex writes a banner (workdir/model/session id) and echoes the whole
    prompt to stderr before the real error, which comes LAST. Naively
    truncating the head of that blob hides the only line that matters, so we
    pull out codex's own ``ERROR:`` lines first and fall back to the tail —
    never the head — of the output.
    """
    blob = (raw_err or raw_out).strip()

    # codex's own error lines, de-duplicated (it often prints them twice)
    errors: list[str] = []
    for ln in blob.splitlines():
        ln = ln.strip()
        if not ln or any(n in ln for n in _NOISE_PATTERNS):
            continue
        if ln.startswith("ERROR:") or ln.startswith("ERROR "):
            text = ln.split(":", 1)[1].strip() if ln.startswith("ERROR:") else ln
            if text not in errors:
                errors.append(text)

    detail = " ".join(errors) if errors else ""

    # Structured API errors carry a JSON "detail"
    if not detail:
        m = re.search(r'"detail":\s*"([^"]+)"', blob)
        if m:
            detail = m.group(1)

    if detail:
        low = detail.lower()
        if "usage limit" in low or "quota" in low or "rate limit" in low:
            when = re.search(r"try again at ([0-9: ]+[AP]M)", detail)
            # Deliberately no "switch provider" advice here: this same
            # message serves the review/Discuss path, which is Codex-specific
            # by design (it exists to get a second opinion from a *different*
            # model), so that remedy would be wrong half the time.
            return (
                "Codex usage limit reached — your ChatGPT/Codex quota is "
                "exhausted"
                + (f"; try again at {when.group(1).strip()}" if when else "")
                + "."
            )
        if "newer version of Codex" in detail or "not supported" in detail:
            return (
                "Codex CLI rejected the model. Run `npm install -g @openai/codex` "
                f"to upgrade, then try again. Original error: {detail}"
            )
        if "not logged in" in low or "unauthorized" in low or "401" in low:
            return "Codex is not signed in. Run `codex login` in a terminal."
        return f"Codex: {detail}"

    # Nothing recognisable — show the TAIL, where the real error lives.
    # Line-based, not character-based: codex echoes the entire prompt as one
    # very long line, so a character slice of the tail would paste chunks of
    # the user's own prompt into the error toast. Real error lines are short;
    # anything over ~300 chars is the prompt echo, not a diagnosis.
    candidates = [
        ln.strip()
        for ln in blob.splitlines()
        if ln.strip()
        and len(ln.strip()) <= 300
        and not any(n in ln for n in _NOISE_PATTERNS)
    ]
    tail = "\n".join(candidates[-4:])[-400:]
    return f"codex exec failed (exit {returncode}): {tail or 'no output'}"


def _is_terminal_failure(explained: str) -> bool:
    """True when retrying as a cold start cannot possibly help (quota, auth)."""
    low = explained.lower()
    return (
        "usage limit" in low
        or "quota" in low
        or "not signed in" in low
        or "rate limit" in low
    )



async def run(prompt: str, *, model: str | None = None) -> tuple[str, str]:
    args = [
        resolve_bin("codex"),
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
        raise RuntimeError(_explain_failure(proc.returncode, raw_err, raw_out))

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
    args: list[str] = [resolve_bin("codex"), "exec"]
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
        # Same extraction as run(): codex puts the real error LAST, after a
        # banner and the echoed prompt, so never truncate from the head.
        explained = _explain_failure(proc.returncode, raw_err, raw_out)
        if session_id_in:
            # A resume can fail because the stored provider session expired,
            # which the caller retries as a cold start — but a quota/auth
            # failure will fail identically on retry, so surface it directly
            # instead of burning a second call.
            if _is_terminal_failure(explained):
                raise RuntimeError(explained)
            raise CodexResumeFailed(explained)
        raise RuntimeError(explained)

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
