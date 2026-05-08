import { describe, expect, it } from "vitest";
import {
  classifyVerdict,
  parseBullets,
  parseCoachReview,
  parseCriticalReview,
  stripFences,
} from "./review-parser";

const SAMPLE_CRITICAL = `VERDICT:
Proceed with caution

KEY FINDINGS:
- The migration is reversible.
- Tests cover happy path only.
- The auto-expand effect can fight user collapse.

MAIN RISK:
The auto-expand effect re-runs after every state change, which can
silently override the user's explicit collapse action.

RECOMMENDED NEXT STEP:
Tighten the guard to only fire when the user hasn't expressed an
opinion yet (persistedOpen === undefined).

NEXT PROMPT FOR CLAUDE CODE:
Update sidebar.tsx so the auto-expand effect only fires when
persistedOpen === undefined. Add a unit test covering "user collapses
a project that contains the active session, state must remain false".

DETAILS:
This applies to the ProjectTreeNode component in
web/src/components/sidebar.tsx around line 117. The bug surfaces when
a user clicks a session inside a project then collapses that project's
header — the click is silently no-oped because the next render cycle
re-expands it.
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

// ---------------------------------------------------------------------------
// parseCriticalReview
// ---------------------------------------------------------------------------

describe("parseCriticalReview", () => {
  it("extracts all sections from a normal reply", () => {
    const r = parseCriticalReview(SAMPLE_CRITICAL);
    expect(r.parsed).toBe(true);
    expect(r.verdict).toBe("caution");
    expect(r.verdictRaw).toBe("Proceed with caution");
    expect(r.keyFindings.length).toBe(3);
    expect(r.keyFindings[0]).toContain("migration is reversible");
    expect(r.keyFindings[2]).toContain("auto-expand");
    expect(r.mainRisk).toContain("auto-expand effect re-runs");
    expect(r.recommendedNextStep).toContain("Tighten the guard");
    expect(r.nextPrompt).toContain("persistedOpen === undefined");
    expect(r.nextPrompt).not.toContain("DETAILS");
    expect(r.details).toContain("ProjectTreeNode component");
  });

  it("handles missing DETAILS section", () => {
    const text = SAMPLE_CRITICAL.replace(/DETAILS:[\s\S]*$/, "").trim();
    const r = parseCriticalReview(text);
    expect(r.parsed).toBe(true);
    expect(r.verdict).toBe("caution");
    expect(r.details).toBeNull();
    expect(r.nextPrompt).toContain("persistedOpen");
  });

  it("strips a fenced code block from NEXT PROMPT FOR CLAUDE CODE", () => {
    const text = `VERDICT:
Good to proceed

NEXT PROMPT FOR CLAUDE CODE:
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

  it("classifies all four verdict phrases", () => {
    const cases: [string, "good" | "caution" | "needs_fix" | "stop"][] = [
      ["Good to proceed", "good"],
      ["Proceed with caution", "caution"],
      ["Needs fix before continuing", "needs_fix"],
      ["Stop and investigate", "stop"],
    ];
    for (const [phrase, expected] of cases) {
      const r = parseCriticalReview(`VERDICT:\n${phrase}\n\nMAIN RISK:\nx`);
      expect(r.verdict).toBe(expected);
    }
  });

  it("falls back to parsed=false on a free-form reply with no headings", () => {
    const r = parseCriticalReview(
      "I think this is pretty good but the tests are thin. Maybe write more tests next.",
    );
    expect(r.parsed).toBe(false);
    expect(r.verdict).toBeNull();
    expect(r.keyFindings).toEqual([]);
    expect(r.nextPrompt).toBeNull();
  });

  it("handles empty / whitespace input without throwing", () => {
    expect(parseCriticalReview("").parsed).toBe(false);
    expect(parseCriticalReview("   \n\t\n").parsed).toBe(false);
  });

  it("tolerates markdown bold around headings", () => {
    const text = `**VERDICT:**
Good to proceed

**KEY FINDINGS:**
- one
- two

**NEXT PROMPT FOR CLAUDE CODE:**
do the thing.
`;
    const r = parseCriticalReview(text);
    expect(r.parsed).toBe(true);
    expect(r.verdict).toBe("good");
    expect(r.keyFindings).toEqual(["one", "two"]);
    expect(r.nextPrompt).toBe("do the thing.");
  });

  it("partial reply with only some sections still parses", () => {
    const text = `VERDICT:\nGood to proceed\n\nNEXT PROMPT FOR CLAUDE CODE:\nproceed.\n`;
    const r = parseCriticalReview(text);
    expect(r.parsed).toBe(true);
    expect(r.verdict).toBe("good");
    expect(r.keyFindings).toEqual([]);
    expect(r.mainRisk).toBeNull();
    expect(r.nextPrompt).toBe("proceed.");
  });
});

// ---------------------------------------------------------------------------
// parseCoachReview
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
    // Half-fenced text shouldn't be mangled.
    const partial = "```\nhello\nworld";
    expect(stripFences(partial)).toBe(partial);
  });
});

describe("classifyVerdict", () => {
  it("returns null on null / empty", () => {
    expect(classifyVerdict(null)).toBeNull();
    expect(classifyVerdict("")).toBeNull();
  });

  it("ignores case and surrounding whitespace", () => {
    expect(classifyVerdict("  good to proceed  ")).toBe("good");
    expect(classifyVerdict("PROCEED WITH CAUTION")).toBe("caution");
  });

  it("returns null for unrecognized phrases", () => {
    expect(classifyVerdict("looking nice")).toBeNull();
  });
});
