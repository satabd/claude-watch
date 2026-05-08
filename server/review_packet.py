"""Review Packet builder for the Review Threads feature.

The Review Packet is the prompt actually sent to the reviewer LLM (Codex in
V1). It is intentionally **forward-focused**: only current evidence and the
user's current question. We never replay old review messages by default.

The builder also produces a compact ``audit_snapshot`` that goes into
``review_messages.evidence_used_json`` so we can later answer "what did the
reviewer actually see?" without storing the full raw evidence forever.
Excerpts are bounded by the ``MAX_*_AUDIT_BYTES`` constants below.

Secret detection scans the prompt-bound text and flags obvious leaks
(API keys, private key headers, etc.) BEFORE the prompt leaves the host.
The user can override on a per-send basis; we do not persist a global
"always allow secrets" preference.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .git_capture import GitCapture

# ---------------------------------------------------------------------------
# Caps. Outbound (sent to reviewer) caps are larger than audit (stored
# forever) caps so the reviewer has enough context but we don't bloat SQLite.
# ---------------------------------------------------------------------------

# Outbound caps
MAX_DIFF_BYTES = 60_000          # mirror git_capture; redundant truncation is harmless
MAX_TEST_OUTPUT_BYTES = 8_000
MAX_BUILD_OUTPUT_BYTES = 8_000
MAX_QUESTION_BYTES = 16_000
MAX_CLAUDE_TURN_BYTES = 12_000
MAX_DIRTY_FILES_LISTED = 50

# Audit caps (stored in evidence_used_json, kept tight)
MAX_DIFF_AUDIT_BYTES = 4_000
MAX_TEST_AUDIT_BYTES = 2_000
MAX_BUILD_AUDIT_BYTES = 2_000
MAX_CLAUDE_TURN_AUDIT_BYTES = 2_000
MAX_QUESTION_AUDIT_BYTES = 2_000
MAX_DIRTY_FILES_AUDITED = 200


# Reviewer modes — keep in sync with REVIEWER_MODE_INSTRUCTIONS below.
REVIEWER_MODES: tuple[str, ...] = ("critical", "prompt_coach")

REVIEWER_MODE_INSTRUCTIONS: dict[str, str] = {
    # Both prompts request a strict, parseable section structure. The frontend
    # parser keys off the literal labels (VERDICT:, KEY FINDINGS:, etc.) to
    # render a compact action-oriented summary; the optional DETAILS section
    # is shown only when the user expands "Show full review". If the model
    # deviates from the structure the frontend silently falls back to the
    # raw view, so a strict format is a UX improvement, not a correctness
    # requirement.
    "critical": (
        "You are a CRITICAL REVIEWER pair-programming with a developer who"
        " is using Claude Code. Reply in a brief, conversational tone — the"
        " user wants a quick decision aid, NOT a formal report.\n"
        "\n"
        "Reply EXACTLY in this format with these section labels (uppercase,"
        " followed by a colon, on their own line). Do not add other"
        " top-level sections by default:\n"
        "\n"
        "VERDICT:\n"
        "[ONE sentence judgment of the work — your overall take, in plain"
        " language]\n"
        "\n"
        "WHY:\n"
        "- [short point]\n"
        "- [short point]\n"
        "- [short point — at most 3 bullets]\n"
        "\n"
        "NEXT ACTION:\n"
        "[ONE clear actionable instruction the user should do next]\n"
        "\n"
        "PROMPT TO SEND CLAUDE:\n"
        "[a copy-ready prompt the user can paste back to Claude Code. No"
        " preamble, no quotes, no fenced code block — just the prompt"
        " text. Focused, specific, and actionable.]\n"
        "\n"
        "Only add a DETAILS: section if the user EXPLICITLY asked for"
        " deeper analysis in their guidance. Otherwise keep your reply"
        " short and chatty — this is a back-and-forth, not a one-shot"
        " writeup."
    ),
    "prompt_coach": (
        "You are a PROMPT COACH helping the user write a stronger prompt for"
        " Claude Code based on the current evidence. Be terse and specific.\n"
        "\n"
        "Reply EXACTLY in this format with these section labels (uppercase,"
        " followed by a colon, on their own line). Do not add other"
        " top-level sections:\n"
        "\n"
        "CLARIFIED INTENT:\n"
        "[1-2 sentences explaining what the user actually wants Claude Code"
        " to do]\n"
        "\n"
        "IMPROVED PROMPT:\n"
        "[the full copy-ready prompt. No preamble, no quotes, no fenced"
        " code block — just the prompt text. Aim for clear scope, success"
        " criteria, and any constraints inferred from the evidence.]\n"
        "\n"
        "WHY THIS IS BETTER:\n"
        "- [short reason 1]\n"
        "- [short reason 2]\n"
        "- [short reason 3 — at most 3 bullets]\n"
        "\n"
        "DETAILS:\n"
        "[optional: longer explanation of context-inclusion choices and"
        " trade-offs. May be omitted entirely.]"
    ),
}


# Secret patterns. Each is (label, compiled_regex). Order is loose to
# specific. We use anchored / structured patterns to keep false-positive
# noise low; the user can always override per send.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "AWS secret access key (env)",
        re.compile(r"AWS_SECRET_ACCESS_KEY\s*=\s*['\"]?[A-Za-z0-9/+=]{30,}"),
    ),
    (
        "GitHub token",
        re.compile(r"\b(?:ghp|gho|ghs|ghr|ghu)_[A-Za-z0-9]{36}\b"),
    ),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b")),
    (
        "Private key block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
    ("OpenAI key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    (
        "Generic credential assignment",
        re.compile(
            r"\b(?:PASSWORD|API[_\-]?KEY|SECRET[_\-]?KEY|ACCESS[_\-]?TOKEN)\s*=\s*['\"]?"
            r"[^\s'\"]{16,}",
            re.IGNORECASE,
        ),
    ),
]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass
class PacketEvidence:
    """User-controlled toggles for what evidence to include."""

    include_claude_turn: bool = True
    include_git_status: bool = True
    include_changed_files: bool = True
    include_git_diff: bool = True
    include_test_output: bool = True
    include_build_output: bool = True


@dataclass
class PacketInputs:
    """Everything the builder needs apart from the GitCapture."""

    question: str
    reviewer_mode: str  # one of REVIEWER_MODES
    project_cwd: str | None = None
    claude_session_id: str | None = None
    claude_turn_uuid: str | None = None
    claude_turn_role: str | None = None
    claude_turn_text: str | None = None
    test_output: str | None = None
    build_output: str | None = None
    evidence: PacketEvidence = field(default_factory=PacketEvidence)


@dataclass(frozen=True)
class SecretHit:
    label: str
    location: str  # which evidence field triggered the hit


@dataclass
class BuiltPacket:
    prompt: str
    byte_count: int
    estimated_tokens: int
    audit_snapshot: dict[str, Any]
    secret_hits: tuple[SecretHit, ...]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trim(text: str, max_bytes: int) -> tuple[str, bool, int]:
    """Trim ``text`` to at most ``max_bytes`` UTF-8 bytes. Returns
    (possibly_trimmed, was_truncated, original_byte_count)."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False, len(raw)
    truncated = raw[:max_bytes].decode("utf-8", errors="ignore")
    return (
        truncated
        + f"\n\n... [truncated; original was {len(raw)} bytes; "
        f"showing first {max_bytes}]",
        True,
        len(raw),
    )


def _excerpt(text: str, max_bytes: int) -> str:
    """Plain head excerpt for the audit snapshot — no marker appended."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _detect_secrets(text: str, location: str) -> list[SecretHit]:
    hits: list[SecretHit] = []
    for label, pat in SECRET_PATTERNS:
        if pat.search(text):
            hits.append(SecretHit(label=label, location=location))
    return hits


def estimate_tokens(byte_count: int) -> int:
    """Cheap UTF-8 byte → token heuristic. Roughly 4 bytes per token in
    English-heavy text; this is good enough for a UI hint and does not need
    a real tokenizer dependency."""
    return (byte_count + 3) // 4


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_packet(
    inputs: PacketInputs,
    git: GitCapture | None,
    *,
    secret_override: bool = False,
) -> BuiltPacket:
    """Build the prompt + audit snapshot + secret report.

    ``secret_override`` does NOT change the secret-hit detection or whether
    they're returned — it only flips the ``secret_override_used`` field in
    the audit snapshot so we can record that the user knowingly proceeded.
    """
    if inputs.reviewer_mode not in REVIEWER_MODES:
        raise ValueError(
            f"unknown reviewer_mode: {inputs.reviewer_mode!r}; "
            f"expected one of {REVIEWER_MODES}"
        )

    parts: list[str] = []
    audit: dict[str, Any] = {}
    secret_hits: list[SecretHit] = []

    parts.append(REVIEWER_MODE_INSTRUCTIONS[inputs.reviewer_mode])
    parts.append("\n--- EVIDENCE ---")

    # Project meta — small, never truncated.
    meta_lines: list[str] = []
    if inputs.project_cwd:
        meta_lines.append(f"PROJECT: {inputs.project_cwd}")
    if inputs.claude_session_id:
        meta_lines.append(f"CLAUDE SESSION: {inputs.claude_session_id}")
    if meta_lines:
        parts.append("\n".join(meta_lines))

    # Selected Claude result.
    if inputs.evidence.include_claude_turn and inputs.claude_turn_text:
        text, truncated, original = _trim(
            inputs.claude_turn_text, MAX_CLAUDE_TURN_BYTES
        )
        header = f"## SELECTED CLAUDE RESULT ({inputs.claude_turn_role or 'turn'})"
        if truncated:
            header += " [truncated]"
        parts.append(f"\n{header}\n{text}")
        audit["claude_turn"] = {
            "included": True,
            "truncated": truncated,
            "byte_count": original,
            "role": inputs.claude_turn_role,
            "uuid": inputs.claude_turn_uuid,
            "excerpt": _excerpt(
                inputs.claude_turn_text, MAX_CLAUDE_TURN_AUDIT_BYTES
            ),
        }
        secret_hits += _detect_secrets(text, "claude_turn")
    else:
        audit["claude_turn"] = {"included": False}

    # Git status (header line + counters).
    if inputs.evidence.include_git_status and git and git.is_repo:
        status_lines = [f"branch: {git.branch or '(detached)'}"]
        if git.ahead or git.behind:
            status_lines.append(
                f"ahead/behind upstream: +{git.ahead}/-{git.behind}"
            )
        status_lines.append(f"dirty entries: {len(git.dirty)}")
        parts.append("\n## GIT STATUS\n" + "\n".join(status_lines))
        audit["git_status"] = {
            "included": True,
            "branch": git.branch,
            "ahead": git.ahead,
            "behind": git.behind,
            "dirty_count": len(git.dirty),
        }
    else:
        audit["git_status"] = {"included": False}

    # Changed files list (capped).
    if (
        inputs.evidence.include_changed_files
        and git
        and git.is_repo
        and git.dirty
    ):
        listed = list(git.dirty)[:MAX_DIRTY_FILES_LISTED]
        rendered = "\n".join(f"  {d.status:>3} {d.path}" for d in listed)
        suffix = (
            f"\n  ... +{len(git.dirty) - MAX_DIRTY_FILES_LISTED} more"
            if len(git.dirty) > MAX_DIRTY_FILES_LISTED
            else ""
        )
        parts.append(f"\n## CHANGED FILES\n{rendered}{suffix}")
        audit["changed_files"] = {
            "included": True,
            "total": len(git.dirty),
            "audited": [
                {"path": d.path, "status": d.status}
                for d in list(git.dirty)[:MAX_DIRTY_FILES_AUDITED]
            ],
            "audited_truncated": len(git.dirty) > MAX_DIRTY_FILES_AUDITED,
        }
    else:
        audit["changed_files"] = {"included": False}

    # Git diff (already pre-trimmed by git_capture, but we re-trim defensively).
    if (
        inputs.evidence.include_git_diff
        and git
        and git.is_repo
        and git.diff
    ):
        diff_text, redundant_trunc, _ = _trim(git.diff, MAX_DIFF_BYTES)
        truncated = git.diff_truncated or redundant_trunc
        header = "## GIT DIFF"
        if truncated:
            header += " (truncated)"
        parts.append(f"\n{header}\n```diff\n{diff_text}\n```")
        audit["git_diff"] = {
            "included": True,
            "truncated": truncated,
            "byte_count": git.diff_byte_count,
            "excerpt": _excerpt(git.diff, MAX_DIFF_AUDIT_BYTES),
        }
        secret_hits += _detect_secrets(diff_text, "git_diff")
    else:
        audit["git_diff"] = {"included": False}

    # Test output (user-pasted).
    if inputs.evidence.include_test_output and inputs.test_output:
        text, truncated, original = _trim(
            inputs.test_output, MAX_TEST_OUTPUT_BYTES
        )
        header = "## TEST OUTPUT"
        if truncated:
            header += " (truncated)"
        parts.append(f"\n{header}\n```\n{text}\n```")
        audit["test_output"] = {
            "included": True,
            "truncated": truncated,
            "byte_count": original,
            "excerpt": _excerpt(inputs.test_output, MAX_TEST_AUDIT_BYTES),
        }
        secret_hits += _detect_secrets(text, "test_output")
    else:
        audit["test_output"] = {"included": False}

    # Build output (user-pasted).
    if inputs.evidence.include_build_output and inputs.build_output:
        text, truncated, original = _trim(
            inputs.build_output, MAX_BUILD_OUTPUT_BYTES
        )
        header = "## BUILD OUTPUT"
        if truncated:
            header += " (truncated)"
        parts.append(f"\n{header}\n```\n{text}\n```")
        audit["build_output"] = {
            "included": True,
            "truncated": truncated,
            "byte_count": original,
            "excerpt": _excerpt(inputs.build_output, MAX_BUILD_AUDIT_BYTES),
        }
        secret_hits += _detect_secrets(text, "build_output")
    else:
        audit["build_output"] = {"included": False}

    # User question — always last; this is what the reviewer is answering.
    q_text, q_truncated, q_original = _trim(
        inputs.question, MAX_QUESTION_BYTES
    )
    parts.append(f"\n--- USER QUESTION ---\n{q_text}")
    audit["question"] = {
        "truncated": q_truncated,
        "byte_count": q_original,
        "excerpt": _excerpt(inputs.question, MAX_QUESTION_AUDIT_BYTES),
    }
    secret_hits += _detect_secrets(q_text, "question")

    prompt = "\n".join(parts)
    byte_count = len(prompt.encode("utf-8"))
    audit["byte_count"] = byte_count
    audit["estimated_tokens"] = estimate_tokens(byte_count)
    audit["reviewer_mode"] = inputs.reviewer_mode
    audit["secret_override_used"] = bool(secret_override and secret_hits)

    return BuiltPacket(
        prompt=prompt,
        byte_count=byte_count,
        estimated_tokens=estimate_tokens(byte_count),
        audit_snapshot=audit,
        secret_hits=tuple(secret_hits),
    )
