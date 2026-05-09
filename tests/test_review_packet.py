"""Tests for server.review_packet — packet builder, secret detection,
audit-snapshot shape."""
from __future__ import annotations

import pytest

from server.git_capture import DirtyEntry, GitCapture
from server.review_packet import (
    MAX_CLAUDE_TURN_BYTES,
    MAX_DIFF_AUDIT_BYTES,
    PacketEvidence,
    PacketInputs,
    build_packet,
    estimate_tokens,
)


def _make_inputs(**overrides) -> PacketInputs:
    base = dict(
        question="Did this change introduce a regression in the parser?",
        skill_id="critical_review",
        project_cwd="/repo/example",
        claude_session_id="sess-123",
        claude_turn_uuid="turn-abc",
        claude_turn_role="assistant",
        claude_turn_text="I refactored parser.py to deduplicate two helpers.",
        test_output=None,
        build_output=None,
        evidence=PacketEvidence(),
    )
    base.update(overrides)
    return PacketInputs(**base)


def _clean_git() -> GitCapture:
    return GitCapture(
        is_repo=True,
        branch="main",
        ahead=0,
        behind=0,
        dirty=(
            DirtyEntry(path="server/parser.py", status=".M"),
            DirtyEntry(path="tests/test_parser.py", status=".M"),
        ),
        diff="diff --git a/server/parser.py b/server/parser.py\n@@ -1 +1 @@\n-old\n+new\n",
        diff_truncated=False,
        diff_byte_count=80,
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_build_critical_packet_includes_all_evidence():
    pkt = build_packet(_make_inputs(), _clean_git())
    assert "CRITICAL REVIEW" in pkt.prompt
    assert "PROJECT: /repo/example" in pkt.prompt
    assert "CLAUDE SESSION: sess-123" in pkt.prompt
    assert "## SELECTED CLAUDE RESULT" in pkt.prompt
    assert "## GIT STATUS" in pkt.prompt
    assert "## CHANGED FILES" in pkt.prompt
    assert "## GIT DIFF" in pkt.prompt
    assert "USER QUESTION" in pkt.prompt
    assert pkt.byte_count == len(pkt.prompt.encode("utf-8"))
    assert pkt.estimated_tokens == estimate_tokens(pkt.byte_count)
    assert pkt.secret_hits == ()


def test_build_quick_review_uses_quick_instructions():
    pkt = build_packet(_make_inputs(skill_id="quick_review"), _clean_git())
    assert "QUICK REVIEW" in pkt.prompt
    assert pkt.audit_snapshot["skill_id"] == "quick_review"
    # Quick Review enforces the canonical chat headings.
    for label in ("VERDICT:", "WHY:", "NEXT ACTION:", "PROMPT TO SEND CLAUDE:"):
        assert label in pkt.prompt


def test_build_prompt_coach_uses_coach_instructions():
    pkt = build_packet(_make_inputs(skill_id="prompt_coach"), _clean_git())
    assert "PROMPT COACH" in pkt.prompt
    assert pkt.audit_snapshot["skill_id"] == "prompt_coach"
    # Legacy alias kept for older consumers reading audit blobs.
    assert pkt.audit_snapshot["reviewer_mode"] == "prompt_coach"


def test_critical_skill_requests_canonical_chat_labels():
    """Lock the contract so a future skill edit doesn't drop a label the
    frontend parser depends on."""
    pkt = build_packet(_make_inputs(skill_id="critical_review"), _clean_git())
    for label in (
        "VERDICT:",
        "WHY:",
        "NEXT ACTION:",
        "PROMPT TO SEND CLAUDE:",
    ):
        assert label in pkt.prompt, f"missing {label!r} in critical prompt"
    # OPTIONAL NOTES is conditional — instructions mention it but don't
    # require it on every reply.
    assert "OPTIONAL NOTES" in pkt.prompt
    # Anti-legacy guard: instructions explicitly forbid old audit
    # headings so the reviewer can't fall back into report mode.
    assert "NEXT PROMPT FOR CLAUDE CODE" in pkt.prompt  # listed as forbidden
    assert "Missing tests" in pkt.prompt or "MISSING TESTS" in pkt.prompt.upper()


def test_coach_skill_uses_unified_prompt_label():
    """All skills now use 'PROMPT TO SEND CLAUDE' — the legacy 'IMPROVED
    PROMPT' label is forbidden in the coach instructions."""
    pkt = build_packet(_make_inputs(skill_id="prompt_coach"), _clean_git())
    assert "CLARIFIED INTENT:" in pkt.prompt
    assert "PROMPT TO SEND CLAUDE:" in pkt.prompt
    assert "WHY THIS WORKS:" in pkt.prompt
    # Forbidden legacy headings appear in the rules section as
    # do-not-use guidance, but the FORMAT block must not use them.
    # We just assert the canonical labels are present.


def test_next_prompt_coach_skill_uses_strategy_partner_labels():
    """The inline Discuss-this-result skill ships its own label set
    so the parser can route the response to the right view variant
    without confusing it with Quick/Critical Review."""
    pkt = build_packet(
        _make_inputs(skill_id="next_prompt_coach"), _clean_git()
    )
    for label in (
        "UNDERSTANDING:",
        "MY TAKE:",
        "NEXT MOVE:",
        "PROMPT TO SEND CLAUDE:",
        "OPTIONAL NOTE:",
    ):
        assert label in pkt.prompt, f"missing {label!r} in next_prompt_coach"
    # Audit snapshot records the skill so post-hoc analysis can answer
    # "which preset produced this exchange?" without inspecting the
    # full prompt body.
    assert pkt.audit_snapshot["skill_id"] == "next_prompt_coach"


def test_unknown_skill_id_raises():
    with pytest.raises(ValueError, match="skill_id"):
        build_packet(_make_inputs(skill_id="banana"), _clean_git())


def test_evidence_toggles_off_excludes_sections():
    inputs = _make_inputs(
        evidence=PacketEvidence(
            include_claude_turn=False,
            include_git_status=False,
            include_changed_files=False,
            include_git_diff=False,
        )
    )
    pkt = build_packet(inputs, _clean_git())
    assert "## SELECTED CLAUDE RESULT" not in pkt.prompt
    assert "## GIT STATUS" not in pkt.prompt
    assert "## CHANGED FILES" not in pkt.prompt
    assert "## GIT DIFF" not in pkt.prompt
    audit = pkt.audit_snapshot
    assert audit["claude_turn"]["included"] is False
    assert audit["git_status"]["included"] is False
    assert audit["changed_files"]["included"] is False
    assert audit["git_diff"]["included"] is False


def test_no_git_capture_when_not_a_repo():
    pkt = build_packet(_make_inputs(), GitCapture(is_repo=False))
    assert "## GIT STATUS" not in pkt.prompt
    assert "## CHANGED FILES" not in pkt.prompt
    assert "## GIT DIFF" not in pkt.prompt
    assert pkt.audit_snapshot["git_status"]["included"] is False


# ---------------------------------------------------------------------------
# Trim / audit snapshot
# ---------------------------------------------------------------------------


def test_long_claude_turn_is_truncated_outbound_but_excerpted_in_audit():
    long_text = "X" * (MAX_CLAUDE_TURN_BYTES + 5_000)
    pkt = build_packet(
        _make_inputs(claude_turn_text=long_text), _clean_git()
    )
    assert "[truncated" in pkt.prompt
    audit = pkt.audit_snapshot["claude_turn"]
    assert audit["truncated"] is True
    assert audit["byte_count"] == len(long_text.encode("utf-8"))
    # Excerpt is bounded — strictly smaller than the original.
    assert len(audit["excerpt"].encode("utf-8")) <= 2_000


def test_long_diff_excerpt_capped_in_audit():
    big_diff = "DIFFLINE\n" * 10_000
    git = GitCapture(
        is_repo=True,
        branch="main",
        diff=big_diff,
        diff_truncated=True,
        diff_byte_count=len(big_diff.encode("utf-8")),
    )
    pkt = build_packet(_make_inputs(), git)
    audit = pkt.audit_snapshot["git_diff"]
    assert audit["truncated"] is True
    assert audit["byte_count"] == len(big_diff.encode("utf-8"))
    assert len(audit["excerpt"].encode("utf-8")) <= MAX_DIFF_AUDIT_BYTES


def test_test_output_truncation_audit_records_original_size():
    big_log = "FAILURE: " * 5_000  # > 8 KB
    pkt = build_packet(
        _make_inputs(test_output=big_log), _clean_git()
    )
    audit = pkt.audit_snapshot["test_output"]
    assert audit["included"] is True
    assert audit["truncated"] is True
    assert audit["byte_count"] == len(big_log.encode("utf-8"))


def test_audit_snapshot_records_byte_count_and_estimated_tokens():
    pkt = build_packet(_make_inputs(), _clean_git())
    assert pkt.audit_snapshot["byte_count"] == pkt.byte_count
    assert pkt.audit_snapshot["estimated_tokens"] == pkt.estimated_tokens
    assert pkt.audit_snapshot["skill_id"] == "critical_review"
    # Legacy alias kept on the audit blob.
    assert pkt.audit_snapshot["reviewer_mode"] == "critical_review"
    assert pkt.audit_snapshot["secret_override_used"] is False


# ---------------------------------------------------------------------------
# Secret detection
# ---------------------------------------------------------------------------


def test_detects_aws_access_key_in_diff():
    git = GitCapture(
        is_repo=True,
        branch="main",
        diff="-old\n+AKIAIOSFODNN7EXAMPLE\n",
        diff_byte_count=40,
    )
    pkt = build_packet(_make_inputs(), git)
    labels = {h.label for h in pkt.secret_hits}
    locations = {h.location for h in pkt.secret_hits}
    assert "AWS access key" in labels
    assert "git_diff" in locations


def test_detects_github_token_in_question():
    pkt = build_packet(
        _make_inputs(question="My token is ghp_" + "a" * 36 + ", thoughts?"),
        _clean_git(),
    )
    assert any(h.label == "GitHub token" for h in pkt.secret_hits)
    assert any(h.location == "question" for h in pkt.secret_hits)


def test_detects_private_key_block_in_test_output():
    pkt = build_packet(
        _make_inputs(test_output="-----BEGIN RSA PRIVATE KEY-----\nABC\n"),
        _clean_git(),
    )
    assert any(h.label == "Private key block" for h in pkt.secret_hits)


def test_secret_override_records_flag_in_audit():
    pkt = build_packet(
        _make_inputs(question="leak ghp_" + "a" * 36),
        _clean_git(),
        secret_override=True,
    )
    assert pkt.secret_hits  # detection still ran
    assert pkt.audit_snapshot["secret_override_used"] is True


def test_secret_override_without_hits_does_not_set_flag():
    # Override without any hit shouldn't claim override was "used".
    pkt = build_packet(_make_inputs(), _clean_git(), secret_override=True)
    assert pkt.secret_hits == ()
    assert pkt.audit_snapshot["secret_override_used"] is False


def test_clean_input_produces_no_secret_hits():
    pkt = build_packet(_make_inputs(), _clean_git())
    assert pkt.secret_hits == ()


# ---------------------------------------------------------------------------
# Token estimator
# ---------------------------------------------------------------------------


def test_estimate_tokens_rough_4_bytes_per_token():
    assert estimate_tokens(0) == 0
    assert estimate_tokens(1) == 1
    assert estimate_tokens(4) == 1
    assert estimate_tokens(5) == 2
    assert estimate_tokens(4000) == 1000
