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
from typing import Final

CODEX_BIN: Final = shutil.which("codex") or "codex"
TIMEOUT_SECONDS = 120


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


def _model_from_metadata(text: str) -> str | None:
    m = _MODEL_RE.search(text)
    if not m:
        return None
    return m.group(1).strip()
