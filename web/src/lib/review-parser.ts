/** Parses a structured reviewer reply into a typed shape the chat-style
 *  panel can render conversationally. The reviewer is asked (in
 *  server/review_packet.py) to use literal uppercase labels followed by a
 *  colon, e.g. ``VERDICT:``, ``WHY:``, ``PROMPT TO SEND CLAUDE:``.
 *
 *  Older replies that used the previous label set (``KEY FINDINGS``,
 *  ``RECOMMENDED NEXT STEP``, ``NEXT PROMPT FOR CLAUDE CODE``) still parse
 *  correctly — the section splitter recognises both, and the field
 *  resolver picks whichever was present.
 *
 *  When the model deviates from the format entirely, the parsers return
 *  ``parsed: false``; the UI then falls back to the raw text view.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface CriticalReview {
  /** Free-form one-sentence verdict. The chat-UX dropped the fixed
   *  enumerated phrase set; the reviewer now writes their own. Also
   *  populated from ``MY TAKE`` (the Next Prompt Coach skill's
   *  equivalent label). */
  verdict: string | null;
  /** Optional context-recap section. Only the Next Prompt Coach skill
   *  emits this today (1-2 sentences explaining what the user is
   *  trying to achieve). The CriticalReviewView renders it as a small
   *  preamble when present; older skills leave it null and the
   *  preamble is skipped. */
  understanding: string | null;
  /** "Why this verdict" — short bullets. Built from the WHY section, or
   *  the legacy KEY FINDINGS section for back-compat. Empty for the
   *  Next Prompt Coach skill (no equivalent section). */
  why: string[];
  /** What the user should do next. Built from NEXT ACTION, the legacy
   *  RECOMMENDED NEXT STEP, or the Next Prompt Coach skill's
   *  ``NEXT MOVE`` label — all three describe the same one-line
   *  next step. */
  nextAction: string | null;
  /** The copy-ready prompt to paste back to Claude Code. Built from
   *  PROMPT TO SEND CLAUDE, or the legacy NEXT PROMPT FOR CLAUDE CODE. */
  nextPrompt: string | null;
  /** Optional deeper explanation. Built from OPTIONAL NOTES (Quick /
   *  Critical), OPTIONAL NOTE (Next Prompt Coach), or the legacy
   *  DETAILS section. Only present when the model has something
   *  worth adding. */
  details: string | null;
  /** True when at least one structured section was found. UI uses this to
   *  decide between the conversational view and the raw fallback. */
  parsed: boolean;
}

export interface CoachReview {
  clarifiedIntent: string | null;
  improvedPrompt: string | null;
  whyThisIsBetter: string[];
  details: string | null;
  parsed: boolean;
}

// ---------------------------------------------------------------------------
// Section splitter
// ---------------------------------------------------------------------------

const KNOWN_HEADINGS = [
  "VERDICT",
  "WHAT MATTERS",
  "NEXT ACTION",
  "PROMPT TO SEND CLAUDE",
  "OPTIONAL NOTES",
  // Next Prompt Coach skill (inline-Discuss default) — its own label
  // set so the splitter terminates each section correctly. The
  // resolvers below map them onto the existing CriticalReview fields:
  // MY TAKE -> verdict, NEXT MOVE -> nextAction, OPTIONAL NOTE ->
  // details. UNDERSTANDING gets its own field on the parsed shape.
  "UNDERSTANDING",
  "MY TAKE",
  "NEXT MOVE",
  "OPTIONAL NOTE",
  // Aliases / back-compat labels — still parsed so older messages render
  // correctly under the new shape.
  "WHY",
  "DETAILS",
  "KEY FINDINGS",
  "MAIN RISK",
  "RECOMMENDED NEXT STEP",
  "NEXT PROMPT FOR CLAUDE CODE",
  // Coach mode labels — current and legacy.
  "CLARIFIED INTENT",
  "WHY THIS WORKS",      // current (matches review_skills.py:_PROMPT_COACH_INSTRUCTION)
  "IMPROVED PROMPT",     // legacy alias for the prompt section
  "WHY THIS IS BETTER",  // legacy alias for the rationale bullets
] as const;

type KnownHeading = (typeof KNOWN_HEADINGS)[number];

/** Strip the cosmetic noise a model might wrap a heading line with
 *  (markdown heading, numeric prefix, bold markers, trailing colon /
 *  dash / period) and uppercase the rest, so a single equality check
 *  matches every plausible variant.
 *
 *  Examples that all normalize to ``NEXT PROMPT FOR CLAUDE CODE``:
 *    * ``NEXT PROMPT FOR CLAUDE CODE:``
 *    * ``## NEXT PROMPT FOR CLAUDE CODE``
 *    * ``6. NEXT PROMPT FOR CLAUDE CODE:``
 *    * ``**6. NEXT PROMPT FOR CLAUDE CODE:**``
 *    * ``### **NEXT PROMPT FOR CLAUDE CODE**``
 *
 *  We loop the strip patterns until the line is stable so noise can
 *  appear in any order — bold-around-numbered, hash-around-bold, etc.
 *  Without the loop, ``**6. …:**`` would only strip the outer bold and
 *  leave the numeric prefix in place. */
function normalizeHeadingLine(line: string): string {
  let s = line.trim();
  for (let i = 0; i < 5; i++) {
    const before = s;
    s = s
      .replace(/^#{1,4}\s+/, "")
      .replace(/^\d+[.)]\s*/, "")
      .replace(/^\*\*\s*/, "")
      .replace(/\s*\*\*$/, "")
      .replace(/[:.\-—]+$/, "")
      .trim();
    if (s === before) break;
  }
  return s.toUpperCase();
}

const KNOWN_SET: ReadonlySet<string> = new Set(KNOWN_HEADINGS);

/** Split a reviewer reply into a Map of heading → trimmed body text. Only
 *  KNOWN_HEADINGS are recognized; other capitalized words on their own
 *  line are treated as body text. Bodies span from the line AFTER the
 *  heading up to (but not including) the next recognized heading. */
function parseSections(text: string): Map<KnownHeading, string> {
  const lines = text.split(/\r?\n/);
  const matches: { heading: KnownHeading; idx: number }[] = [];
  for (let i = 0; i < lines.length; i++) {
    const norm = normalizeHeadingLine(lines[i]);
    if (KNOWN_SET.has(norm)) {
      matches.push({ heading: norm as KnownHeading, idx: i });
    }
  }
  const out = new Map<KnownHeading, string>();
  for (let m = 0; m < matches.length; m++) {
    const start = matches[m].idx + 1;
    const end = m + 1 < matches.length ? matches[m + 1].idx : lines.length;
    const body = lines.slice(start, end).join("\n").trim();
    const existing = out.get(matches[m].heading);
    // If we see the same heading twice (rare), keep the longer body.
    if (!existing || body.length > existing.length) {
      out.set(matches[m].heading, body);
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Bullet parsing
// ---------------------------------------------------------------------------

/** Parse a section body into a list of bullets. Tolerant of:
 *  - leading "-", "*", "•" or "1." / "1)" markers
 *  - multi-line bullets (continuation lines without a marker are appended
 *    to the previous bullet, joined by a space)
 *  - bullet-less prose (treated as a single-bullet list) */
export function parseBullets(text: string, max = 6): string[] {
  if (!text) return [];
  const out: string[] = [];
  let current: string | null = null;
  for (const raw of text.split(/\r?\n/)) {
    const m = raw.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (m) {
      if (current !== null) out.push(current.trim());
      current = m[1];
    } else if (raw.trim()) {
      if (current !== null) {
        current += " " + raw.trim();
      } else {
        out.push(raw.trim());
      }
    }
  }
  if (current !== null) out.push(current.trim());
  return out.filter(Boolean).slice(0, max);
}

// ---------------------------------------------------------------------------
// Code-fence stripper
// ---------------------------------------------------------------------------

/** If the body is wrapped in a single fenced code block (```…```), return
 *  the inner content; otherwise return the input unchanged. Used on the
 *  next-prompt and improved-prompt sections so the reviewer can write
 *  ```text … ``` and we still produce a clean copyable string. */
export function stripFences(text: string): string {
  const m = text.trim().match(/^```[a-zA-Z0-9_-]*\n([\s\S]*?)\n```$/);
  return m ? m[1].trim() : text.trim();
}

// ---------------------------------------------------------------------------
// Public parsers
// ---------------------------------------------------------------------------

/** Parse a Critical Reviewer reply. Returns a fully-shaped object whose
 *  ``parsed`` flag is true iff at least one structured section was
 *  recognized; the UI falls back to the raw view when ``parsed`` is
 *  false. */
export function parseCriticalReview(text: string): CriticalReview {
  if (!text || !text.trim()) {
    return {
      verdict: null,
      understanding: null,
      why: [],
      nextAction: null,
      nextPrompt: null,
      details: null,
      parsed: false,
    };
  }
  const sections = parseSections(text);

  // Understanding is unique to the Next Prompt Coach skill. Older
  // skills (Quick / Critical) don't emit it, in which case we leave
  // the field null and the view skips the preamble.
  const understanding = sections.get("UNDERSTANDING")?.trim() || null;

  // Verdict is the leading one-liner. Quick / Critical use VERDICT;
  // Next Prompt Coach calls the same role MY TAKE. Both map onto this
  // single field — the view renders one bold sentence either way.
  const verdictBody =
    sections.get("VERDICT")?.trim() ||
    sections.get("MY TAKE")?.trim() ||
    null;
  const verdict = verdictBody && verdictBody.length > 0 ? verdictBody : null;

  const whyBody =
    sections.get("WHAT MATTERS") ??
    sections.get("WHY") ??
    sections.get("KEY FINDINGS") ??
    "";
  const why = parseBullets(whyBody, 5);

  // NEXT ACTION / NEXT MOVE / RECOMMENDED NEXT STEP all describe the
  // same one-line next step; the parser surfaces them as one field so
  // the view's "Next: …" line works for every skill. MAIN RISK is
  // not exposed as its own field anymore; if the model wrote it, fold
  // it in so the user still sees the content.
  const nextActionBody =
    sections.get("NEXT ACTION") ??
    sections.get("NEXT MOVE") ??
    sections.get("RECOMMENDED NEXT STEP") ??
    null;
  const mainRiskBody = sections.get("MAIN RISK") ?? null;
  const nextAction =
    nextActionBody && nextActionBody.trim()
      ? nextActionBody.trim()
      : mainRiskBody && mainRiskBody.trim()
      ? mainRiskBody.trim()
      : null;

  const rawNextPrompt =
    sections.get("PROMPT TO SEND CLAUDE") ??
    sections.get("NEXT PROMPT FOR CLAUDE CODE") ??
    null;
  const nextPrompt =
    rawNextPrompt && rawNextPrompt.trim() ? stripFences(rawNextPrompt) : null;

  // OPTIONAL NOTES (Quick/Critical, plural) and OPTIONAL NOTE
  // (Next Prompt Coach, singular) are the same role; legacy DETAILS
  // is still recognized for back-compat.
  const details =
    sections.get("OPTIONAL NOTES")?.trim() ||
    sections.get("OPTIONAL NOTE")?.trim() ||
    sections.get("DETAILS")?.trim() ||
    null;

  const parsed = !!(
    verdict ||
    understanding ||
    why.length ||
    nextAction ||
    nextPrompt
  );
  return {
    verdict,
    understanding,
    why,
    nextAction,
    nextPrompt,
    details,
    parsed,
  };
}

/** Parse a Prompt Coach reply. Same conventions as parseCriticalReview. */
export function parseCoachReview(text: string): CoachReview {
  if (!text || !text.trim()) {
    return {
      clarifiedIntent: null,
      improvedPrompt: null,
      whyThisIsBetter: [],
      details: null,
      parsed: false,
    };
  }
  const sections = parseSections(text);
  const clarifiedIntent = sections.get("CLARIFIED INTENT")?.trim() || null;
  // Coach skill switched from "IMPROVED PROMPT" to the unified
  // "PROMPT TO SEND CLAUDE" label when the skill registry landed.
  // Prefer the new label; fall back to the legacy one for messages
  // produced before the switch.
  const rawImproved =
    sections.get("PROMPT TO SEND CLAUDE")?.trim() ||
    sections.get("IMPROVED PROMPT")?.trim() ||
    null;
  const improvedPrompt = rawImproved ? stripFences(rawImproved) : null;
  // Coach skill renamed "WHY THIS IS BETTER" -> "WHY THIS WORKS" when
  // the skill registry landed. Prefer the new label; fall back to the
  // legacy one for older messages.
  const whyThisIsBetter = parseBullets(
    sections.get("WHY THIS WORKS") ??
      sections.get("WHY THIS IS BETTER") ??
      "",
    5,
  );
  const details = sections.get("DETAILS")?.trim() || null;
  const parsed = !!(clarifiedIntent || improvedPrompt || whyThisIsBetter.length);
  return {
    clarifiedIntent,
    improvedPrompt,
    whyThisIsBetter,
    details,
    parsed,
  };
}
