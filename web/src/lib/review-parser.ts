/** Parses a structured reviewer reply (Critical Reviewer / Prompt Coach)
 *  into a typed shape the panel can render as a compact action-oriented
 *  summary. The reviewer is asked (in server/review_packet.py) to use
 *  literal uppercase labels followed by a colon, e.g. ``VERDICT:``,
 *  ``KEY FINDINGS:``, ``NEXT PROMPT FOR CLAUDE CODE:``.
 *
 *  When the model deviates from the format, the parsers return ``parsed:
 *  false``; the UI then falls back to the raw text view, while still
 *  using the legacy ``copyTargetForReply`` helper to extract a
 *  copy-ready prompt heuristically.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type Verdict = "good" | "caution" | "needs_fix" | "stop";

export interface CriticalReview {
  /** Normalized 4-state verdict, or null when the reply didn't contain a
   *  recognizable phrase. */
  verdict: Verdict | null;
  /** The raw verdict line as the reviewer wrote it, for display. */
  verdictRaw: string | null;
  keyFindings: string[];
  mainRisk: string | null;
  recommendedNextStep: string | null;
  nextPrompt: string | null;
  details: string | null;
  /** True when at least one of the structured sections was found. UI uses
   *  this to decide between the compact view and the raw fallback view. */
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
// Verdict labels (UI-facing copy + tone for badge color)
// ---------------------------------------------------------------------------

export const VERDICT_DISPLAY: Record<Verdict, { label: string; tone: "ok" | "warn" | "danger" | "stop" }> = {
  good: { label: "Good to proceed", tone: "ok" },
  caution: { label: "Proceed with caution", tone: "warn" },
  needs_fix: { label: "Needs fix before continuing", tone: "danger" },
  stop: { label: "Stop and investigate", tone: "stop" },
};

// ---------------------------------------------------------------------------
// Section splitter
// ---------------------------------------------------------------------------

const KNOWN_HEADINGS = [
  "VERDICT",
  "KEY FINDINGS",
  "MAIN RISK",
  "RECOMMENDED NEXT STEP",
  "NEXT PROMPT FOR CLAUDE CODE",
  "DETAILS",
  "CLARIFIED INTENT",
  "IMPROVED PROMPT",
  "WHY THIS IS BETTER",
] as const;

type KnownHeading = (typeof KNOWN_HEADINGS)[number];

/** Strip the cosmetic noise a model might wrap a heading line with
 *  (markdown heading, numeric prefix, bold markers, trailing colon /
 *  dash / period) and uppercase the rest, so a single equality check
 *  matches every plausible variant — ``**VERDICT:**``, ``## Verdict``,
 *  ``1. VERDICT —``, ``Verdict:`` all collapse to ``VERDICT``. */
function normalizeHeadingLine(line: string): string {
  return line
    .trim()
    .replace(/^#{1,4}\s+/, "")
    .replace(/^\d+[.)]\s*/, "")
    .replace(/^\*\*\s*/, "")
    .replace(/\s*\*\*$/, "")
    .replace(/[:.\-—]+$/, "")
    .trim()
    .toUpperCase();
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
 *  - bullet-less prose (treated as a single-bullet list)
 *
 *  Caps the result at ``max`` to enforce the spec's "at most 3 bullets" UX. */
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
// Verdict classifier
// ---------------------------------------------------------------------------

const VERDICT_KEYWORDS: { v: Verdict; keys: string[] }[] = [
  { v: "good", keys: ["good to proceed"] },
  { v: "caution", keys: ["proceed with caution"] },
  { v: "needs_fix", keys: ["needs fix", "needs fixing", "fix before continuing"] },
  { v: "stop", keys: ["stop and investigate", "stop", "investigate"] },
];

export function classifyVerdict(raw: string | null): Verdict | null {
  if (!raw) return null;
  const t = raw.toLowerCase();
  for (const { v, keys } of VERDICT_KEYWORDS) {
    for (const k of keys) {
      if (t.includes(k)) return v;
    }
  }
  return null;
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
      verdictRaw: null,
      keyFindings: [],
      mainRisk: null,
      recommendedNextStep: null,
      nextPrompt: null,
      details: null,
      parsed: false,
    };
  }
  const sections = parseSections(text);
  const verdictBody = sections.get("VERDICT")?.trim() || null;
  // Verdict is a single line; if the model wrote multiple, take the first
  // non-empty one for classification but keep the body for display.
  const verdictLine = verdictBody?.split(/\r?\n/).find((ln) => ln.trim()) ?? null;
  const verdict = classifyVerdict(verdictLine);
  const keyFindings = parseBullets(sections.get("KEY FINDINGS") ?? "", 5);
  const mainRisk = sections.get("MAIN RISK")?.trim() || null;
  const recommendedNextStep = sections.get("RECOMMENDED NEXT STEP")?.trim() || null;
  const rawNextPrompt = sections.get("NEXT PROMPT FOR CLAUDE CODE")?.trim() || null;
  const nextPrompt = rawNextPrompt ? stripFences(rawNextPrompt) : null;
  const details = sections.get("DETAILS")?.trim() || null;
  const parsed = !!(
    verdict ||
    verdictLine ||
    keyFindings.length ||
    mainRisk ||
    recommendedNextStep ||
    nextPrompt
  );
  return {
    verdict,
    verdictRaw: verdictLine,
    keyFindings,
    mainRisk,
    recommendedNextStep,
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
  const rawImproved = sections.get("IMPROVED PROMPT")?.trim() || null;
  const improvedPrompt = rawImproved ? stripFences(rawImproved) : null;
  const whyThisIsBetter = parseBullets(sections.get("WHY THIS IS BETTER") ?? "", 5);
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
