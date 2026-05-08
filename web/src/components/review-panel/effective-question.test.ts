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

  it("default question mentions the structured review surface", () => {
    // Sanity guard: if someone changes the default, they should think about
    // whether it still asks for a 'next best prompt for Claude Code'.
    expect(DEFAULT_QUESTION.toLowerCase()).toContain("next best prompt");
    expect(DEFAULT_QUESTION.toLowerCase()).toContain("claude code");
  });
});
