/** Pure helpers extracted from review-panel.tsx so the inline-discussion
 *  code path (Phase C) can import them without dragging in the panel's
 *  React surface. No JSX here.
 *
 *  These are used to:
 *    1. Identify which Skill produced a stored reviewer message — needed
 *       to pick the right parser + render variant. Older messages may
 *       lack the new ``skill_id`` field; we fall back through legacy
 *       ``reviewer_mode`` and ultimately to ``critical_review``.
 *    2. Map a Skill to a render mode. Quick + Critical share one
 *       conversational view; Prompt Coach has its own.
 */
import type { ReviewMessage, SkillId } from "@/lib/api";

/** Render variant a UI surface should use for a given Skill. */
export type ReviewerRenderMode = "critical_or_quick" | "prompt_coach";

/** Pull the skill id that was used when this message was sent. Reads the
 *  new ``skill_id`` field first, then the legacy ``reviewer_mode`` for
 *  back-compat with messages from before the skill system, then falls
 *  back to ``critical_review`` (the default for ambiguous legacy data).
 *  Returns the skill_id; the caller maps it to a render mode. */
export function reviewerModeFromMessage(m: ReviewMessage): SkillId {
  const ctx = m.context_used_json as
    | { skill_id?: string; reviewer_mode?: string }
    | null;
  const skill = ctx?.skill_id;
  if (
    skill === "next_prompt_coach" ||
    skill === "quick_review" ||
    skill === "critical_review" ||
    skill === "prompt_coach"
  ) {
    return skill;
  }
  const legacy = ctx?.reviewer_mode;
  if (legacy === "next_prompt_coach") return "next_prompt_coach";
  if (legacy === "quick_review") return "quick_review";
  if (legacy === "prompt_coach") return "prompt_coach";
  if (legacy === "critical_review" || legacy === "critical") {
    return "critical_review";
  }
  return "critical_review";
}

/** Resolve the render mode for a message. Critical, Quick Review, and
 *  Next Prompt Coach all use the same compact view (verdict-style
 *  leading sentence + optional understanding preamble + bullets +
 *  next-action line + prompt + optional notes). Only Prompt Coach has
 *  a different shape (clarified intent + improved prompt + reasons). */
export function renderModeForSkill(skill: SkillId): ReviewerRenderMode {
  return skill === "prompt_coach" ? "prompt_coach" : "critical_or_quick";
}
