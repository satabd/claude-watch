"""LLM provider abstraction.

Each provider exposes a `run(prompt, *, model=None) -> (text, model_used)` async fn.
Providers shell out to a CLI that already has auth (claude OAuth or ChatGPT Codex).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable

from . import claude_provider, codex_provider

ProviderFn = Callable[..., Awaitable[tuple[str, str]]]

PROVIDERS: dict[str, ProviderFn] = {
    "claude": claude_provider.run,
    "codex": codex_provider.run,
}

# Per-provider default models for "fast" tier (translate/summarize/glossary)
# vs "smart" tier (clarify/explain).
DEFAULT_MODELS: dict[str, dict[str, str | None]] = {
    "claude": {
        "fast": "claude-haiku-4-5-20251001",
        "smart": "claude-sonnet-4-6",
    },
    "codex": {
        # None → let codex use its configured default model
        "fast": None,
        "smart": None,
    },
}


_log = logging.getLogger("watcher.providers")

_DEFAULT_MAX_CONCURRENCY = 2
_ENV_VAR = "WATCHER_PROVIDER_MAX_CONCURRENCY"


def _resolve_max_concurrency() -> int:
    raw = os.environ.get(_ENV_VAR)
    if raw is None or raw == "":
        return _DEFAULT_MAX_CONCURRENCY
    try:
        n = int(raw)
        if n <= 0:
            raise ValueError("must be positive")
        return n
    except (ValueError, TypeError):
        _log.warning(
            "invalid %s=%r; falling back to default %d",
            _ENV_VAR,
            raw,
            _DEFAULT_MAX_CONCURRENCY,
        )
        return _DEFAULT_MAX_CONCURRENCY


MAX_CONCURRENCY = _resolve_max_concurrency()
_log.info("provider concurrency limit: %d", MAX_CONCURRENCY)

# asyncio.Semaphore binds to the running event loop on first use. Lazily create
# one per loop so this works in tests and in apps that may use multiple loops.
_semaphores: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _get_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        _semaphores[loop] = sem
    return sem


def _wrap(name: str, fn: ProviderFn) -> ProviderFn:
    async def wrapped(prompt, *, model=None):
        sem = _get_semaphore()
        if sem.locked():
            _log.debug("provider %s waiting for concurrency slot", name)
        async with sem:
            return await fn(prompt, model=model)

    wrapped.__wrapped__ = fn  # type: ignore[attr-defined]
    return wrapped


class _GatedProviders(dict):
    """Dict that returns a semaphore-wrapped callable for whatever underlying
    provider is currently registered under the key. Wrapping at lookup time
    (rather than at module init) means tests and runtime can mutate
    PROVIDERS[...] = some_fn and the gating still applies."""

    def __getitem__(self, key):
        fn = super().__getitem__(key)
        # If already wrapped (idempotency for re-lookups), return as-is.
        if getattr(fn, "__watcher_gated__", False):
            return fn
        wrapped = _wrap(key, fn)
        wrapped.__watcher_gated__ = True  # type: ignore[attr-defined]
        return wrapped

    def get(self, key, default=None):
        if super().__contains__(key):
            return self.__getitem__(key)
        return default


_raw = PROVIDERS
PROVIDERS = _GatedProviders(_raw)  # type: ignore[assignment]


def resolve(provider: str, tier: str = "fast") -> tuple[ProviderFn, str | None]:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    model = DEFAULT_MODELS.get(provider, {}).get(tier)
    return PROVIDERS[provider], model
