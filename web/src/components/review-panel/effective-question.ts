/** Pure helpers used by ReviewPanel for the empty-question UX.
 *
 *  Lives in its own module so unit tests can import them without pulling
 *  in the React + zustand surface of review-panel.tsx.
 */

/** Used when the user clicks Send without typing a question. Keeping this
 *  one literal in one place means the size preview, the actual /send call,
 *  and any future test all agree on what gets sent. */
export const DEFAULT_QUESTION =
  "Please review the selected Claude result, current evidence, risks, missing tests, and suggest the next best prompt for Claude Code.";

/** Frontend never calls /api/reviews/{preview,send} with an empty question
 *  — the backend's pydantic validator requires min_length=1 and would
 *  return a raw 422. We always substitute DEFAULT_QUESTION when the user
 *  hasn't typed (or only typed whitespace). */
export function effectiveQuestion(raw: string): string {
  const trimmed = raw.trim();
  return trimmed || DEFAULT_QUESTION;
}
