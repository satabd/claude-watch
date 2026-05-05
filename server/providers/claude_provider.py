"""Claude provider — shells out to `claude -p`."""
from __future__ import annotations

import asyncio
import shutil
from typing import Final

CLAUDE_BIN: Final = shutil.which("claude") or "claude"
TIMEOUT_SECONDS = 90


async def run(prompt: str, *, model: str | None = None) -> tuple[str, str]:
    args = [CLAUDE_BIN, "-p"]
    if model:
        args += ["--model", model]
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
        raise RuntimeError("claude CLI timed out")
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {err[:500]}")
    return stdout.decode("utf-8", errors="replace").strip(), model or "claude"
