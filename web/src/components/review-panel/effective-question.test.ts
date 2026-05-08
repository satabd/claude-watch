import { describe, expect, it } from "vitest";
import {
  DEFAULT_QUESTION,
  effectiveQuestion,
} from "./effective-question";

describe("effectiveQuestion", () => {
  it("returns the typed question unchanged when non-empty", () => {
    expect(effectiveQuestion("anything specific?")).toBe(
      "anything specific?",
    );
  });

  it("trims surrounding whitespace from a real question", () => {
    expect(effectiveQuestion("   please look here   ")).toBe(
      "please look here",
    );
  });

  it("substitutes the default when the input is empty", () => {
    expect(effectiveQuestion("")).toBe(DEFAULT_QUESTION);
  });

  it("substitutes the default when the input is only whitespace", () => {
    expect(effectiveQuestion("   \t\n  ")).toBe(DEFAULT_QUESTION);
  });

  it("never returns an empty string", () => {
    for (const raw of ["", " ", "\n", "\t\t", "  \n  ", "x"]) {
      expect(effectiveQuestion(raw).length).toBeGreaterThan(0);
    }
  });

  it("default question asks for a next prompt to send Claude Code", () => {
    // Sanity guard: if someone changes the default, they should think about
    // whether it still asks for a copy-ready prompt to send back.
    const lower = DEFAULT_QUESTION.toLowerCase();
    expect(lower).toContain("next prompt");
    expect(lower).toContain("claude code");
  });
});
