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
#
# Claude entries use the CLI's *aliases* rather than dated ids. `claude
# --model sonnet` always resolves to the current Sonnet, so the app does not
# silently keep calling a superseded snapshot every time a new one ships —
# and there is no dated string to go stale in this file.
DEFAULT_MODELS: dict[str, dict[str, str | None]] = {
    "claude": {
        "fast": "haiku",
        "smart": "sonnet",
    },
    "codex": {
        # None → let codex use its configured default model
        "fast": None,
        "smart": None,
    },
}

# Models offerable per provider, in ascending capability/cost. `id` is passed
# straight to the CLI's --model flag; every one of these is verified to be
# accepted by `claude --model <id>`.
AVAILABLE_MODELS: dict[str, list[dict[str, str]]] = {
    "claude": [
        {
            "id": "haiku",
            "label": "Haiku",
            "note": "Fastest and cheapest. Good for translation and summaries.",
        },
        {
            "id": "sonnet",
            "label": "Sonnet",
            "note": "Balanced. Noticeably better at explanation and code.",
        },
        {
            "id": "opus",
            "label": "Opus",
            "note": "Most capable, slowest and priciest.",
        },
    ],
    # Codex model selection lives in the codex CLI's own config, not here.
    "codex": [],
}

# Which tiers the UI lets you choose a model for, and what each one drives.
TIERS: list[dict[str, str]] = [
    {
        "key": "fast",
        "label": "Fast",
        "note": "Translate, summarize, glossary — high volume, short output.",
    },
    {
        "key": "smart",
        "label": "Smart",
        "note": "Clarify, explain, prompt writer — reasoning-heavy.",
    },
]


def model_choices(provider: str) -> list[dict[str, str]]:
    return AVAILABLE_MODELS.get(provider, [])


def is_valid_model(provider: str, model: str) -> bool:
    return any(m["id"] == model for m in AVAILABLE_MODELS.get(provider, []))


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


def resolve(
    provider: str, tier: str = "fast", *, override: str | None = None
) -> tuple[ProviderFn, str | None]:
    """Pick the callable and model for a provider/tier.

    ``override`` is the user's saved model choice for this tier. It wins over
    the built-in default, but only if it is still a model we offer — a stale
    setting (model retired, provider switched) falls back rather than passing
    a dead id to the CLI.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    if override and is_valid_model(provider, override):
        return PROVIDERS[provider], override
    model = DEFAULT_MODELS.get(provider, {}).get(tier)
    return PROVIDERS[provider], model


# ---------------------------------------------------------------------------
# Review-mode providers
# ---------------------------------------------------------------------------
#
# A separate dispatch table for the Review Threads feature. Each entry is a
# coroutine that takes ``(prompt, *, session_id_in, model)`` and returns a
# provider-specific ``Result`` dataclass. Codex is the only V1 reviewer, but
# we keep the indirection so adding Gemini / Claude self-review later is
# additive (new provider module + register it here, no churn elsewhere).

REVIEW_PROVIDERS: dict[str, Callable[..., Awaitable]] = {
    "codex": codex_provider.run_review,
}


def is_review_provider(name: str) -> bool:
    return name in REVIEW_PROVIDERS
