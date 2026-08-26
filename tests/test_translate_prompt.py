"""Guardrails for the translation prompt.

The original prompt ("You are a precise translator") primed word-for-word
output: `pushed the fix` became a literal calque and pane/transcript got
Arabic coinages developers never use. The rewrite translates meaning first.
These tests pin the properties that fixed it, so a future edit can't quietly
regress them.
"""
from __future__ import annotations

from server.actions import TRANSLATE_TEMPLATE


def _rendered(lang="Arabic"):
    return TRANSLATE_TEMPLATE.format(lang_code="ar", lang_name=lang, text="X")


def test_meaning_first_not_word_for_word():
    assert "Translate the meaning, not the words" in TRANSLATE_TEMPLATE
    assert "precise translator" not in TRANSLATE_TEMPLATE


def test_standard_register_no_dialect():
    # Sonnet drifted into Egyptian dialect until the register was pinned.
    t = TRANSLATE_TEMPLATE
    assert "never a regional dialect" in t
    assert "فصحى" in t  # Arabic gets the explicit MSA name


def test_tech_terms_rule_beats_register():
    # Pinning فصحى suppressed the keep-English rule until precedence was
    # stated outright, with the exact failure as the example.
    t = TRANSLATE_TEMPLATE
    assert "wins over register" in t
    assert "push" in t and "pane" in t
    assert "دفعت الإصلاح" in t  # the literal calque, shown as the wrong answer


def test_hard_constraints_survive():
    t = TRANSLATE_TEMPLATE
    assert "Output ONLY the translation" in t
    assert "leave the code untouched" in t
    assert "URLs" in t


def test_placeholders():
    out = _rendered()
    assert "{lang_name}" not in out and "{text}" not in out
    assert "X" in out
