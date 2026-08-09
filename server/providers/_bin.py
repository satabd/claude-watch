"""Locate provider CLI binaries at call time, with a clear error.

Resolving with ``shutil.which(...)`` at *import* time is unreliable: when the
server is started by launchd (the always-on LaunchAgent) the process inherits a
minimal PATH that excludes Homebrew, so the lookup fails and the bare command
name is spawned instead — surfacing as an opaque ``FileNotFoundError`` from deep
inside asyncio. Resolving lazily also means a CLI installed *after* the server
started is picked up without a restart.

``EXTRA_PATHS`` covers the usual install locations so an inherited-PATH gap
degrades to "works anyway" rather than "every AI action 500s".
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

# Common CLI install locations, checked after PATH.
EXTRA_PATHS: tuple[str, ...] = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    str(Path.home() / ".local" / "bin"),
    str(Path.home() / "Library" / "pnpm"),
    str(Path.home() / ".cargo" / "bin"),
    str(Path.home() / "bin"),
)


class ProviderBinaryNotFound(RuntimeError):
    """Raised when a provider's CLI can't be located on this machine."""


def resolve_bin(name: str) -> str:
    """Return an executable path for ``name``.

    Honors a ``<NAME>_BIN`` environment override (e.g. ``CLAUDE_BIN``) before
    searching PATH and then ``EXTRA_PATHS``.
    """
    override = os.environ.get(f"{name.upper()}_BIN")
    if override:
        return override

    found = shutil.which(name)
    if found:
        return found

    for d in EXTRA_PATHS:
        cand = Path(d) / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)

    raise ProviderBinaryNotFound(
        f"The '{name}' CLI was not found. Install it, or set {name.upper()}_BIN "
        f"to its full path. (Searched PATH plus: {', '.join(EXTRA_PATHS)}). "
        f"If the server runs as a LaunchAgent, note that launchd uses a minimal "
        f"PATH — see service/run.sh."
    )
