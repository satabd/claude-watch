"""Review Skills — behavior presets for the reviewer LLM.

A *skill* defines the reviewer's role, response style, allowed output
format, and the legacy headings it must avoid. Skills are versioned: when
the instruction body changes meaningfully, bump :data:`SKILL_VERSION` so
existing provider sessions (which may still echo the old format) are
discarded and a fresh session is started.

V1 ships three skills; ``quick_review`` is the default.

The route layer maps the legacy ``reviewer_mode`` field to the new
``skill_id`` so older clients and stored ``review_messages`` rows keep
working.
"""
from __future__ import annotations

from dataclasses import dataclass


# Bump when ANY skill's instruction body changes in a way the reviewer
# would notice. The send route refuses to resume a Codex session that
# was created against a different skill_id or version, so a bump
# guarantees a clean break from old-format replies.
#
# v2: add next_prompt_coach (the inline-Discuss default). Existing
# Codex sessions stored under v1 are discarded on next send so the new
# skill's instruction isn't mixed with v1 memory.
SKILL_VERSION: int = 2

# Default in the side panel. The inline-Discuss surface picks
# ``next_prompt_coach`` directly — see review-panel/inline-discussion.
DEFAULT_SKILL_ID = "quick_review"


@dataclass(frozen=True)
class ReviewSkill:
    id: str
    label: str
    purpose: str
    instruction: str


# ---------------------------------------------------------------------------
# Skill instructions — every skill enforces the same canonical headings so
# the frontend parser keys off ONE set across all of them. Legacy headings
# (``KEY FINDINGS``, ``RECOMMENDED NEXT STEP``, ``NEXT PROMPT FOR CLAUDE
# CODE``, ``IMPROVED PROMPT``, ``WHY THIS IS BETTER``) are explicitly
# *forbidden* in the new instructions, then mapped by the parser as a
# back-compat layer for messages stored before this skill system existed.
# ---------------------------------------------------------------------------

_QUICK_REVIEW_INSTRUCTION = (
    "You are doing a QUICK REVIEW of the current Claude Code result. Be"
    " brief and action-oriented. Focus on the SELECTED CLAUDE RESULT and"
    " the user's current guidance.\n"
    "\n"
    "Rules:\n"
    "- Be brief. Do NOT write a formal audit report.\n"
    "- Do NOT use legacy audit headings such as 'What looks correct',\n"
    "  'Risks / weak assumptions', 'Missing tests', 'Scope creep warnings',\n"
    "  'Recommended next step', or 'NEXT PROMPT FOR CLAUDE CODE'. Use\n"
    "  ONLY the labels listed below.\n"
    "- Always include PROMPT TO SEND CLAUDE.\n"
    "- Do not lecture about parser implementation, UI implementation, or\n"
    "  tests UNLESS that is the actual subject of the Claude result.\n"
    "\n"
    "Reply EXACTLY in this format. Each label is uppercase, followed by a"
    " colon, on its own line. Do not add other top-level sections by"
    " default:\n"
    "\n"
    "VERDICT:\n"
    "[ONE short sentence — your overall take]\n"
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
    "[a clean, copy-ready prompt the user can paste back to Claude Code."
    " Format as Markdown when structure helps — preserve numbered steps,"
    " bullets, and fenced code blocks. No preamble, no surrounding fences"
    " around the WHOLE prompt, no meta commentary.]\n"
    "\n"
    "OPTIONAL NOTES:\n"
    "[ONLY if there is something the user genuinely needs to know that"
    " doesn't fit above. Otherwise omit this section entirely.]"
)


_CRITICAL_REVIEW_INSTRUCTION = (
    "You are doing a CRITICAL REVIEW of the current Claude Code result."
    " Be skeptical but concise. Focus on risks, weak assumptions, missing"
    " tests, and scope creep — always against the SELECTED CLAUDE RESULT"
    " and the user's current guidance.\n"
    "\n"
    "Rules:\n"
    "- Be skeptical but stay concise.\n"
    "- Do NOT write a long report unless the user asks.\n"
    "- Do NOT use legacy audit headings such as 'What looks correct',\n"
    "  'Missing tests', 'Scope creep warnings', 'Recommended next step',\n"
    "  'KEY FINDINGS', or 'NEXT PROMPT FOR CLAUDE CODE'. Use ONLY the labels\n"
    "  listed below.\n"
    "- Always include PROMPT TO SEND CLAUDE.\n"
    "- Do not lecture about parser implementation, UI implementation, or\n"
    "  tests UNLESS that is the actual subject of the Claude result.\n"
    "\n"
    "Reply EXACTLY in this format. Each label is uppercase, followed by a"
    " colon, on its own line. Do not add other top-level sections by"
    " default:\n"
    "\n"
    "VERDICT:\n"
    "[ONE short sentence]\n"
    "\n"
    "WHY:\n"
    "- [risk or finding]\n"
    "- [risk or finding]\n"
    "- [risk or finding — at most 3 bullets]\n"
    "\n"
    "NEXT ACTION:\n"
    "[ONE clear actionable instruction]\n"
    "\n"
    "PROMPT TO SEND CLAUDE:\n"
    "[a clean, copy-ready prompt. Markdown when structure helps; no outer"
    " fences around the WHOLE prompt.]\n"
    "\n"
    "OPTIONAL NOTES:\n"
    "[ONLY if needed; otherwise omit this section entirely.]"
)


_PROMPT_COACH_INSTRUCTION = (
    "You are a PROMPT COACH. Help the user write the best next prompt for"
    " Claude Code based on the SELECTED CLAUDE RESULT and their current"
    " guidance. Clarify intention, then produce a stronger prompt. Do not"
    " over-explain.\n"
    "\n"
    "Rules:\n"
    "- Do NOT use legacy headings 'IMPROVED PROMPT', 'WHY THIS IS\n"
    "  BETTER', or 'NEXT PROMPT FOR CLAUDE CODE'. Use ONLY the labels\n"
    "  listed below — every skill in this product uses 'PROMPT TO SEND\n"
    "  CLAUDE'.\n"
    "- Be terse and specific.\n"
    "\n"
    "Reply EXACTLY in this format. Each label is uppercase, followed by a"
    " colon, on its own line:\n"
    "\n"
    "CLARIFIED INTENT:\n"
    "[1-2 sentences explaining what the user actually wants Claude Code"
    " to do.]\n"
    "\n"
    "PROMPT TO SEND CLAUDE:\n"
    "[the full copy-ready prompt. Markdown when structure helps; no outer"
    " fences around the WHOLE prompt; just the prompt text.]\n"
    "\n"
    "WHY THIS WORKS:\n"
    "- [short reason]\n"
    "- [short reason]"
)


# Default skill for the inline "Discuss this result" surface. Behaves as
# a prompt strategy partner — interprets the user's intention, gives a
# practical take on the Claude result, and produces a copy-ready next
# prompt. Distinct from Quick Review (audit-flavored) and Prompt Coach
# (pure prompt-writing) because the inline workflow is "I have a Claude
# result, help me think about it AND write the next prompt."
_NEXT_PROMPT_COACH_INSTRUCTION = (
    "You are the NEXT PROMPT COACH for a developer using Claude Code."
    " Your job is to be a prompt strategy partner — NOT a code auditor.\n"
    "Help the user understand what they're trying to do, give a practical"
    " take on the current Claude Code result, and produce a clean"
    " copy-ready prompt for the next step.\n"
    "\n"
    "Rules:\n"
    "- Be conversational but concise.\n"
    "- Do NOT write a formal audit report.\n"
    "- Do NOT over-focus on tests unless testing is obviously the next\n"
    "  step.\n"
    "- Do NOT simply summarize Claude's work — interpret it.\n"
    "- Infer the user's intention from the SELECTED CLAUDE RESULT and\n"
    "  their guidance.\n"
    "- If the direction is over-engineered, say so clearly. Prefer a\n"
    "  narrow next action over broad architecture.\n"
    "- Always produce a copy-ready PROMPT TO SEND CLAUDE.\n"
    "- Do NOT use legacy audit headings such as 'VERDICT', 'WHY',\n"
    "  'WHAT MATTERS', 'KEY FINDINGS', 'RECOMMENDED NEXT STEP', or\n"
    "  'NEXT PROMPT FOR CLAUDE CODE'. Use ONLY the labels listed below.\n"
    "\n"
    "Reply EXACTLY in this format. Each label is uppercase, followed by"
    " a colon, on its own line:\n"
    "\n"
    "UNDERSTANDING:\n"
    "[1-2 sentences explaining what the user is trying to achieve.]\n"
    "\n"
    "MY TAKE:\n"
    "[short practical opinion on the current Claude result and direction:"
    " good direction / too complex / needs narrowing / ready to proceed."
    " 1-3 sentences.]\n"
    "\n"
    "NEXT MOVE:\n"
    "[ONE clear next action.]\n"
    "\n"
    "PROMPT TO SEND CLAUDE:\n"
    "[a copy-ready prompt the user can paste back to Claude Code.\n"
    " Required quality:\n"
    "  - Specific and actionable.\n"
    "  - Includes constraints (what to keep, what to preserve).\n"
    "  - When scope control is needed, explicitly state what NOT to do.\n"
    "  - Requests verification or results (tests, build, output to\n"
    "    paste back).\n"
    "  - NOT a generic summary of the work.\n"
    "  - Not unnecessarily long. Match length to task complexity.\n"
    " Format as Markdown when structure helps — preserve numbered steps,\n"
    " bullets, and fenced code blocks for code or commands. No preamble,\n"
    " no surrounding fences around the WHOLE prompt.]\n"
    "\n"
    "OPTIONAL NOTE:\n"
    "[ONLY if there is something the user genuinely needs to know that"
    " doesn't fit above. Otherwise omit this section entirely.]"
)


SKILLS: dict[str, ReviewSkill] = {
    "next_prompt_coach": ReviewSkill(
        id="next_prompt_coach",
        label="Next Prompt Coach",
        purpose=(
            "Prompt strategy partner: interpret the Claude result and "
            "produce the next prompt."
        ),
        instruction=_NEXT_PROMPT_COACH_INSTRUCTION,
    ),
    "quick_review": ReviewSkill(
        id="quick_review",
        label="Quick Review",
        purpose="Fast daily review of the current Claude result.",
        instruction=_QUICK_REVIEW_INSTRUCTION,
    ),
    "critical_review": ReviewSkill(
        id="critical_review",
        label="Critical Review",
        purpose="Find risks, weak assumptions, missing tests, scope creep.",
        instruction=_CRITICAL_REVIEW_INSTRUCTION,
    ),
    "prompt_coach": ReviewSkill(
        id="prompt_coach",
        label="Prompt Coach",
        purpose="Help write the best next prompt for Claude Code.",
        instruction=_PROMPT_COACH_INSTRUCTION,
    ),
}


# Legacy ``reviewer_mode`` values from clients / messages predating skills.
LEGACY_REVIEWER_MODE_TO_SKILL: dict[str, str] = {
    "critical": "critical_review",
    "prompt_coach": "prompt_coach",
    # No mapping for "quick_review" since it's the new default; older
    # clients sending "critical" become Critical Review for parity.
}


def resolve_skill_id(
    skill_id: str | None, legacy_reviewer_mode: str | None = None
) -> str:
    """Resolve an incoming request to a known skill id.

    Order of precedence:
      1. ``skill_id`` if it's a known skill.
      2. ``legacy_reviewer_mode`` if mapped to a known skill.
      3. :data:`DEFAULT_SKILL_ID`.
    """
    if skill_id and skill_id in SKILLS:
        return skill_id
    if legacy_reviewer_mode and legacy_reviewer_mode in LEGACY_REVIEWER_MODE_TO_SKILL:
        return LEGACY_REVIEWER_MODE_TO_SKILL[legacy_reviewer_mode]
    return DEFAULT_SKILL_ID


def get_skill(skill_id: str) -> ReviewSkill:
    """Return the skill for ``skill_id``. Raises ``ValueError`` if unknown.

    Routes call :func:`resolve_skill_id` first to guarantee the lookup
    succeeds; this is the strict variant for internal call-sites that
    have already validated the input."""
    if skill_id not in SKILLS:
        raise ValueError(
            f"unknown skill_id: {skill_id!r}; expected one of {tuple(SKILLS)}"
        )
    return SKILLS[skill_id]
