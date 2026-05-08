import { describe, expect, it } from "vitest";
import {
  parseBullets,
  parseCoachReview,
  parseCriticalReview,
  stripFences,
} from "./review-parser";

const SAMPLE_CRITICAL = `VERDICT:
The change looks good but the test coverage is thin.

WHY:
- The migration is reversible.
- Tests cover only the happy path.
- The auto-expand effect can fight user collapse.

NEXT ACTION:
Tighten the auto-expand guard so it only fires when the user
hasn't expressed an opinion yet (persistedOpen === undefined).

PROMPT TO SEND CLAUDE:
Update sidebar.tsx so the auto-expand effect only fires when
persistedOpen === undefined. Add a unit test covering "user collapses
a project that contains the active session, state must remain false".
`;

const SAMPLE_COACH = `CLARIFIED INTENT:
The user wants to refactor parser.py to deduplicate two similar helper
functions while keeping behavior identical.

IMPROVED PROMPT:
Refactor server/parser.py to deduplicate the two helper functions
_extract_user_text and _extract_assistant_text. Preserve current
behavior verbatim and add a parametrized pytest case that exercises
both code paths with identical input.

WHY THIS IS BETTER:
- Names the exact file and functions, not just "parser stuff".
- States the behavioral invariant (verbatim) so Claude doesn't
  overreach.
- Asks for a parametrized test, which prevents regressions silently.

DETAILS:
The previous prompt said "clean up the parser" which is too vague.
`;

// Older format that predates the chat-UX overhaul. Verifies back-compat:
// new field names map onto the legacy section labels.
const SAMPLE_CRITICAL_LEGACY = `VERDICT:
Proceed with caution

KEY FINDINGS:
- old finding 1
- old finding 2

MAIN RISK:
A subtle race when the watcher reconnects mid-poll.

RECOMMENDED NEXT STEP:
Add a retry counter on the SSH connection.

NEXT PROMPT FOR CLAUDE CODE:
Add a retry counter, capped at 5, with exponential backoff.
`;

// ---------------------------------------------------------------------------
// parseCriticalReview — current chat-UX format
// ---------------------------------------------------------------------------

describe("parseCriticalReview (chat format)", () => {
  it("extracts verdict / why / nextAction / nextPrompt", () => {
    const r = parseCriticalReview(SAMPLE_CRITICAL);
    expect(r.parsed).toBe(true);
    expect(r.verdict).toContain("test coverage is thin");
    expect(r.why.length).toBe(3);
    expect(r.why[0]).toContain("migration is reversible");
    expect(r.why[2]).toContain("auto-expand effect");
    expect(r.nextAction).toContain("Tighten the auto-expand guard");
    expect(r.nextPrompt).toContain("persistedOpen === undefined");
    expect(r.nextPrompt).not.toContain("PROMPT TO SEND CLAUDE");
    // DETAILS isn't required in the chat format — should be null here.
    expect(r.details).toBeNull();
  });

  it("verdict is the free-form section body, not a fixed enum", () => {
    const r = parseCriticalReview(SAMPLE_CRITICAL);
    // No classifier; verdict is whatever the model wrote.
    expect(typeof r.verdict).toBe("string");
    expect(r.verdict).not.toBe("good");
    expect(r.verdict).not.toBe("caution");
  });

  it("strips fenced code blocks from PROMPT TO SEND CLAUDE", () => {
    const text = `VERDICT:
Looks fine.

PROMPT TO SEND CLAUDE:
\`\`\`text
Add a smoke test that opens the panel with no threads and asserts
auto-create runs exactly once.
\`\`\`
`;
    const r = parseCriticalReview(text);
    expect(r.nextPrompt).toBe(
      "Add a smoke test that opens the panel with no threads and asserts\nauto-create runs exactly once.",
    );
    expect(r.nextPrompt).not.toContain("```");
  });

  it("optional DETAILS section is captured when the model includes it", () => {
    const text = SAMPLE_CRITICAL + "\nDETAILS:\nMore explanation here.\n";
    const r = parseCriticalReview(text);
    expect(r.details).toContain("More explanation");
  });

  it("partial reply (verdict only) still parses", () => {
    const text = `VERDICT:\nLooks good.\n`;
    const r = parseCriticalReview(text);
    expect(r.parsed).toBe(true);
    expect(r.verdict).toBe("Looks good.");
    expect(r.why).toEqual([]);
    expect(r.nextPrompt).toBeNull();
  });

  it("free-form reply with no labels → parsed=false, all fields null/empty", () => {
    const r = parseCriticalReview(
      "I think this is fine; maybe write more tests.",
    );
    expect(r.parsed).toBe(false);
    expect(r.verdict).toBeNull();
    expect(r.why).toEqual([]);
    expect(r.nextAction).toBeNull();
    expect(r.nextPrompt).toBeNull();
  });

  it("empty / whitespace input doesn't throw", () => {
    expect(parseCriticalReview("").parsed).toBe(false);
    expect(parseCriticalReview("   \n\t").parsed).toBe(false);
  });

  it("tolerates markdown bold around headings", () => {
    const text = `**VERDICT:**
Looks good.

**WHY:**
- one
- two

**PROMPT TO SEND CLAUDE:**
do the thing.
`;
    const r = parseCriticalReview(text);
    expect(r.parsed).toBe(true);
    expect(r.verdict).toBe("Looks good.");
    expect(r.why).toEqual(["one", "two"]);
    expect(r.nextPrompt).toBe("do the thing.");
  });
});

// ---------------------------------------------------------------------------
// parseCriticalReview — legacy back-compat
// ---------------------------------------------------------------------------

describe("parseCriticalReview (chat format with WHAT MATTERS / OPTIONAL NOTES)", () => {
  it("recognizes WHAT MATTERS as the bullets section", () => {
    const text = `VERDICT:
Looks safe.

WHAT MATTERS:
- the migration is reversible
- the diff is small

NEXT ACTION:
Ship it.

PROMPT TO SEND CLAUDE:
Add a smoke test that exercises the rollback path.
`;
    const r = parseCriticalReview(text);
    expect(r.parsed).toBe(true);
    expect(r.why).toEqual([
      "the migration is reversible",
      "the diff is small",
    ]);
    expect(r.nextAction).toBe("Ship it.");
    expect(r.nextPrompt).toContain("smoke test");
  });

  it("recognizes OPTIONAL NOTES as the details section", () => {
    const text = `VERDICT:\nFine.\n\nOPTIONAL NOTES:\nWatch the timeout config under heavy load.\n`;
    const r = parseCriticalReview(text);
    expect(r.details).toContain("Watch the timeout");
  });

  it("preserves Markdown structure inside the prompt section", () => {
    const text = `VERDICT:
Needs structure.

PROMPT TO SEND CLAUDE:
Refactor the auth flow as follows:

1. Add a \`requireAuth\` middleware in src/auth.ts.
2. Update routes:
   - GET /api/users
   - POST /api/sessions

\`\`\`ts
export const requireAuth = (req, res, next) => { /* … */ };
\`\`\`

Make sure existing tests still pass.
`;
    const r = parseCriticalReview(text);
    expect(r.nextPrompt).toBeTruthy();
    // The parser should NOT collapse line breaks or strip inner markdown.
    expect(r.nextPrompt).toContain("1. Add a `requireAuth`");
    expect(r.nextPrompt).toContain("- GET /api/users");
    expect(r.nextPrompt).toContain("```ts");
    expect(r.nextPrompt).toContain("```");
    // Newlines are preserved.
    expect(r.nextPrompt!.split("\n").length).toBeGreaterThan(5);
  });
});

describe("parseCriticalReview (heading variants — robust normalization)", () => {
  it("handles bold-around-numbered legacy heading **6. NEXT PROMPT FOR CLAUDE CODE:**", () => {
    const text = `VERDICT:
Looks fine.

**6. NEXT PROMPT FOR CLAUDE CODE:**
Add a smoke test for the empty-list path.
`;
    const r = parseCriticalReview(text);
    expect(r.parsed).toBe(true);
    expect(r.nextPrompt).toBe("Add a smoke test for the empty-list path.");
  });

  it("handles markdown heading legacy form ### NEXT PROMPT FOR CLAUDE CODE", () => {
    const text = `VERDICT:
Looks fine.

### NEXT PROMPT FOR CLAUDE CODE
Restructure the diff.
`;
    const r = parseCriticalReview(text);
    expect(r.parsed).toBe(true);
    expect(r.nextPrompt).toBe("Restructure the diff.");
  });

  it("handles numbered legacy form '6. NEXT PROMPT FOR CLAUDE CODE:'", () => {
    const text = `VERDICT:
Looks fine.

6. NEXT PROMPT FOR CLAUDE CODE:
Tighten the guard.
`;
    const r = parseCriticalReview(text);
    expect(r.parsed).toBe(true);
    expect(r.nextPrompt).toBe("Tighten the guard.");
  });
});

describe("parseCriticalReview (legacy format back-compat)", () => {
  it("maps KEY FINDINGS → why and RECOMMENDED NEXT STEP → nextAction", () => {
    const r = parseCriticalReview(SAMPLE_CRITICAL_LEGACY);
    expect(r.parsed).toBe(true);
    expect(r.verdict).toBe("Proceed with caution");
    expect(r.why).toEqual(["old finding 1", "old finding 2"]);
    expect(r.nextAction).toContain("retry counter");
  });

  it("maps NEXT PROMPT FOR CLAUDE CODE → nextPrompt", () => {
    const r = parseCriticalReview(SAMPLE_CRITICAL_LEGACY);
    expect(r.nextPrompt).toContain("retry counter, capped at 5");
  });

  it("falls back to MAIN RISK when no NEXT ACTION / RECOMMENDED NEXT STEP", () => {
    const text = `VERDICT:\nLooks fine.\n\nMAIN RISK:\nA race.\n`;
    const r = parseCriticalReview(text);
    expect(r.nextAction).toBe("A race.");
  });
});

// ---------------------------------------------------------------------------
// parseCoachReview (unchanged — coach format kept the same)
// ---------------------------------------------------------------------------

describe("parseCoachReview", () => {
  it("extracts all sections from a normal coach reply", () => {
    const r = parseCoachReview(SAMPLE_COACH);
    expect(r.parsed).toBe(true);
    expect(r.clarifiedIntent).toContain("deduplicate");
    expect(r.improvedPrompt).toContain("server/parser.py");
    expect(r.improvedPrompt).not.toMatch(/^```/);
    expect(r.whyThisIsBetter.length).toBe(3);
    expect(r.whyThisIsBetter[0]).toContain("Names the exact file");
    expect(r.details).toContain("too vague");
  });

  it("missing DETAILS section yields null details but parsed=true", () => {
    const text = SAMPLE_COACH.replace(/DETAILS:[\s\S]*$/, "").trim();
    const r = parseCoachReview(text);
    expect(r.parsed).toBe(true);
    expect(r.details).toBeNull();
    expect(r.improvedPrompt).toContain("server/parser.py");
  });

  it("strips fenced code block from IMPROVED PROMPT", () => {
    const text = `CLARIFIED INTENT:\nx\n\nIMPROVED PROMPT:\n\`\`\`\nDo a thing.\n\`\`\`\n`;
    const r = parseCoachReview(text);
    expect(r.improvedPrompt).toBe("Do a thing.");
  });

  it("falls back to parsed=false on free-form reply", () => {
    const r = parseCoachReview("Just write a clearer prompt next time.");
    expect(r.parsed).toBe(false);
  });

  it("handles empty input without throwing", () => {
    expect(parseCoachReview("").parsed).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Helper exports
// ---------------------------------------------------------------------------

describe("parseBullets", () => {
  it("parses dashed list", () => {
    expect(parseBullets("- one\n- two\n- three")).toEqual([
      "one",
      "two",
      "three",
    ]);
  });

  it("parses asterisked + numbered list", () => {
    expect(parseBullets("* one\n2. two\n3) three")).toEqual([
      "one",
      "two",
      "three",
    ]);
  });

  it("joins continuation lines into the previous bullet", () => {
    const text = "- first finding\n  continued on next line\n- second finding";
    expect(parseBullets(text)).toEqual([
      "first finding continued on next line",
      "second finding",
    ]);
  });

  it("handles bullet-less prose as a single entry", () => {
    expect(parseBullets("just one paragraph")).toEqual(["just one paragraph"]);
  });

  it("caps at the requested max", () => {
    const text = "- a\n- b\n- c\n- d\n- e\n- f\n- g";
    expect(parseBullets(text, 3)).toEqual(["a", "b", "c"]);
  });

  it("returns [] on empty input", () => {
    expect(parseBullets("")).toEqual([]);
    expect(parseBullets("   ")).toEqual([]);
  });
});

describe("stripFences", () => {
  it("removes ```text fences", () => {
    expect(stripFences("```text\nhello\n```")).toBe("hello");
  });

  it("removes plain ``` fences", () => {
    expect(stripFences("```\nhello\n```")).toBe("hello");
  });

  it("returns input unchanged when no fences", () => {
    expect(stripFences("hello")).toBe("hello");
  });

  it("only strips when the WHOLE body is a single fence", () => {
    const partial = "```\nhello\nworld";
    expect(stripFences(partial)).toBe(partial);
  });
});
