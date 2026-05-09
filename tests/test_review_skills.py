"""Tests for server.review_skills — registry, defaults, legacy mapping."""
from __future__ import annotations

from server.review_skills import (
    DEFAULT_SKILL_ID,
    LEGACY_REVIEWER_MODE_TO_SKILL,
    SKILL_VERSION,
    SKILLS,
    get_skill,
    resolve_skill_id,
)


def test_registry_ships_four_skills():
    """V1 shipped three; v2 added next_prompt_coach (the inline-Discuss
    default — strategy partner rather than auditor)."""
    assert set(SKILLS.keys()) == {
        "next_prompt_coach",
        "quick_review",
        "critical_review",
        "prompt_coach",
    }


def test_default_panel_skill_is_quick_review():
    """The side panel's default stays Quick Review — the inline
    Discuss surface picks next_prompt_coach itself."""
    assert DEFAULT_SKILL_ID == "quick_review"
    assert DEFAULT_SKILL_ID in SKILLS


def test_skill_version_bumped_to_two():
    """v2 introduced the next_prompt_coach skill. Bumping the version
    forces existing Codex sessions (created under v1) to be discarded
    on next send so old reviewer behavior doesn't bleed in via
    resume memory."""
    assert isinstance(SKILL_VERSION, int)
    assert SKILL_VERSION == 2


def test_each_skill_has_a_label_and_purpose():
    for skill in SKILLS.values():
        assert skill.label.strip()
        assert skill.purpose.strip()
        assert skill.instruction.strip()


def test_next_prompt_coach_uses_strategy_partner_format():
    """Lock the contract: the inline-Discuss skill must use the new
    UNDERSTANDING / MY TAKE / NEXT MOVE / PROMPT TO SEND CLAUDE labels
    and explicitly forbid the legacy audit headings so it can't drift
    back into Quick Review behavior."""
    body = SKILLS["next_prompt_coach"].instruction
    for label in (
        "UNDERSTANDING:",
        "MY TAKE:",
        "NEXT MOVE:",
        "PROMPT TO SEND CLAUDE:",
        "OPTIONAL NOTE:",
    ):
        assert label in body, f"missing {label!r} in next_prompt_coach"
    # Anti-audit guard: the new skill must explicitly tell the model
    # not to fall back into Quick/Critical Review's structure.
    assert "VERDICT" in body
    assert "WHY" in body
    assert "NEXT PROMPT FOR CLAUDE CODE" in body
    # Strategy-partner posture (not auditor): explicit cue lines.
    assert "prompt strategy partner" in body
    assert "NOT a code auditor" in body
    # Prompt-quality instructions: the model must be told to produce
    # specific, scoped, verification-requesting prompts.
    lower = body.lower()
    assert "specific" in lower and "actionable" in lower
    assert "constraint" in lower or "constraints" in lower
    assert "verif" in lower  # "verification" or "verify"


def test_quick_and_critical_use_canonical_chat_headings():
    """Quick Review and Critical Review must both use the canonical chat
    headings — VERDICT, WHY, NEXT ACTION, PROMPT TO SEND CLAUDE — so the
    frontend renders them with the same compact view."""
    for skill_id in ("quick_review", "critical_review"):
        body = SKILLS[skill_id].instruction
        for label in ("VERDICT:", "WHY:", "NEXT ACTION:", "PROMPT TO SEND CLAUDE:"):
            assert label in body, f"{skill_id!r} missing {label!r}"


def test_prompt_coach_uses_canonical_prompt_label():
    """Coach must use 'PROMPT TO SEND CLAUDE' (unified across skills),
    NOT the legacy 'IMPROVED PROMPT' label."""
    body = SKILLS["prompt_coach"].instruction
    assert "CLARIFIED INTENT:" in body
    assert "PROMPT TO SEND CLAUDE:" in body
    assert "WHY THIS WORKS:" in body


def test_skills_explicitly_forbid_legacy_audit_headings():
    """Lock the anti-legacy guard so the reviewer can't fall back into the
    old audit-report style. The instruction body must list these as
    do-not-use, even though they're still parsed for back-compat on
    historical messages."""
    for skill_id in ("quick_review", "critical_review"):
        body = SKILLS[skill_id].instruction
        # 'NEXT PROMPT FOR CLAUDE CODE' is the most important legacy
        # heading to forbid: parser tolerates it, but skills must not
        # generate it.
        assert "NEXT PROMPT FOR CLAUDE CODE" in body
    coach = SKILLS["prompt_coach"].instruction
    assert "IMPROVED PROMPT" in coach


def test_resolve_skill_id_passthrough():
    assert resolve_skill_id("quick_review") == "quick_review"
    assert resolve_skill_id("critical_review") == "critical_review"
    assert resolve_skill_id("prompt_coach") == "prompt_coach"


def test_resolve_skill_id_falls_back_to_default_for_unknown():
    assert resolve_skill_id("banana") == DEFAULT_SKILL_ID
    assert resolve_skill_id(None) == DEFAULT_SKILL_ID
    assert resolve_skill_id("") == DEFAULT_SKILL_ID


def test_resolve_skill_id_maps_legacy_reviewer_mode():
    # No new skill_id, but legacy reviewer_mode given → use the mapping.
    assert resolve_skill_id(None, "critical") == "critical_review"
    assert resolve_skill_id(None, "prompt_coach") == "prompt_coach"


def test_resolve_skill_id_skill_takes_priority_over_legacy():
    # If the client sends both, the new skill_id wins.
    assert resolve_skill_id("prompt_coach", "critical") == "prompt_coach"


def test_resolve_skill_id_unknown_legacy_falls_back():
    assert resolve_skill_id(None, "banana") == DEFAULT_SKILL_ID


def test_legacy_mapping_only_covers_known_old_modes():
    """No new skills should accidentally land in the legacy map — that
    map is for client-side back-compat, not for new behavior."""
    assert set(LEGACY_REVIEWER_MODE_TO_SKILL.keys()) == {
        "critical",
        "prompt_coach",
    }


def test_get_skill_strict():
    skill = get_skill("quick_review")
    assert skill.id == "quick_review"
    assert skill.label == "Quick Review"
