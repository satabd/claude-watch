"""LLM provider abstraction.

Each provider exposes a `run(prompt, *, model=None) -> (text, model_used)` async fn.
Providers shell out to a CLI that already has auth (claude OAuth or ChatGPT Codex).
"""
from __future__ import annotations

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


def resolve(provider: str, tier: str = "fast") -> tuple[ProviderFn, str | None]:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    model = DEFAULT_MODELS.get(provider, {}).get(tier)
    return PROVIDERS[provider], model
