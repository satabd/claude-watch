"""Project-wide logging configuration.

We configure only the ``watcher.*`` logger hierarchy so we don't fight with
uvicorn's own logger setup. Child loggers (``watcher.db``, ``watcher.remote``,
``watcher.providers``, ``watcher.local``, ``watcher.routes``) propagate to
the ``watcher`` parent which has our handler + level; ``propagate=False``
on the parent means our lines go straight to stderr without being re-emitted
through the root logger / uvicorn's formatter.

Level controlled by ``WATCHER_LOG_LEVEL`` (default ``INFO``). Valid names:
DEBUG / INFO / WARNING / ERROR / CRITICAL.

Call ``configure_logging()`` exactly once, before any submodule that emits
log lines at module-import time (notably ``server.providers.__init__`` which
logs the concurrency limit on import, and ``server.remote_watcher`` which
logs the tunables). The right place is the very top of ``server/main.py``.
"""
from __future__ import annotations

import logging
import os
import sys


_DEFAULT_LEVEL = "INFO"
_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_FORMAT = "%(asctime)s %(name)-22s %(levelname)-7s %(message)s"
_DATEFMT = "%H:%M:%S"

# Marker we set on the parent logger so re-imports during e.g. uvicorn
# --reload don't pile up duplicate handlers.
_CONFIGURED_FLAG = "_watcher_configured"


def _resolve_level() -> int:
    raw = os.environ.get("WATCHER_LOG_LEVEL", _DEFAULT_LEVEL).strip().upper()
    if raw in _VALID_LEVELS:
        return getattr(logging, raw)
    # Can't use logging here — we're in the middle of configuring it.
    print(
        f"WATCHER_LOG_LEVEL={raw!r} is invalid; using {_DEFAULT_LEVEL}. "
        f"Valid: {sorted(_VALID_LEVELS)}",
        file=sys.stderr,
    )
    return getattr(logging, _DEFAULT_LEVEL)


def configure_logging() -> None:
    """Set up the ``watcher`` parent logger. Idempotent.

    No-op on the second call (e.g. uvicorn --reload re-importing this module),
    so handlers don't accumulate.
    """
    parent = logging.getLogger("watcher")
    if getattr(parent, _CONFIGURED_FLAG, False):
        return

    level = _resolve_level()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler.setLevel(level)

    # Clear any handlers a previous (incomplete) attempt may have added.
    for h in list(parent.handlers):
        parent.removeHandler(h)

    parent.setLevel(level)
    parent.addHandler(handler)
    # Don't re-emit through the root logger / uvicorn's formatter.
    parent.propagate = False

    setattr(parent, _CONFIGURED_FLAG, True)

    parent.info(
        "logging configured: level=%s (override via WATCHER_LOG_LEVEL)",
        logging.getLevelName(level),
    )
