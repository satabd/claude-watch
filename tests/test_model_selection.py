"""Per-tier model selection.

Models used to be hardcoded (haiku for fast, sonnet for smart) with no way to
change them. These cover the override path and, importantly, that a stale or
bogus saved choice can never reach the CLI as a dead --model argument.
"""
from __future__ import annotations

import pytest

from server import db, providers


def test_claude_offers_haiku_sonnet_opus():
    ids = [m["id"] for m in providers.model_choices("claude")]
    assert ids == ["haiku", "sonnet", "opus"]


def test_defaults_use_aliases_not_dated_ids():
    """Aliases track the current model; a dated id silently goes stale."""
    for tier in ("fast", "smart"):
        model = providers.DEFAULT_MODELS["claude"][tier]
        assert model in {"haiku", "sonnet", "opus"}
        assert "-20" not in model  # no date-stamped snapshot


def test_resolve_without_override_uses_default():
    _, model = providers.resolve("claude", "fast")
    assert model == "haiku"


def test_override_wins():
    _, model = providers.resolve("claude", "fast", override="sonnet")
    assert model == "sonnet"


def test_invalid_override_falls_back_rather_than_passing_dead_id():
    _, model = providers.resolve("claude", "fast", override="gpt-4")
    assert model == "haiku"
    _, model = providers.resolve("claude", "smart", override="claude-sonnet-4-6")
    assert model == "sonnet"  # retired dated id is not offered → falls back


def test_empty_override_falls_back():
    """Clearing a choice stores "" — must not become --model ''."""
    _, model = providers.resolve("claude", "smart", override="")
    assert model == "sonnet"


def test_is_valid_model():
    assert providers.is_valid_model("claude", "opus") is True
    assert providers.is_valid_model("claude", "nope") is False
    assert providers.is_valid_model("codex", "opus") is False


def test_setting_round_trip(isolated_db):
    db.set_setting("model_claude_fast", "opus")
    override = db.get_setting("model_claude_fast", None)
    _, model = providers.resolve("claude", "fast", override=override)
    assert model == "opus"
