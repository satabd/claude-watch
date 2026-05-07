import { describe, expect, it } from "vitest";
import { copyTargetForReply, extractNextPrompt } from "./extract-next-prompt";

describe("extractNextPrompt", () => {
  it("extracts the section after the canonical heading", () => {
    const reply = [
      "## What looks correct",
      "Refactor is sound.",
      "",
      "## NEXT PROMPT FOR CLAUDE CODE",
      "Add a test for the empty-list edge case in parser.py.",
    ].join("\n");
    expect(extractNextPrompt(reply)).toBe(
      "Add a test for the empty-list edge case in parser.py.",
    );
  });

  it("handles numbered heading (e.g. '6. NEXT PROMPT…')", () => {
    const reply = [
      "1. What looks correct: stuff",
      "6. NEXT PROMPT FOR CLAUDE CODE:",
      "Restructure the diff to avoid the unrelated formatting change.",
      "",
      "End of reply.",
    ].join("\n");
    const got = extractNextPrompt(reply);
    expect(got).toContain("Restructure the diff");
    // "End of reply." is plain text, not a heading boundary, so it's
    // included. That matches the user's expectation: when there's no
    // following heading, the prompt is the whole tail.
  });

  it("stops at the next markdown heading", () => {
    const reply = [
      "## NEXT PROMPT FOR CLAUDE CODE",
      "Run the failing test in isolation and post output.",
      "",
      "## Footer",
      "Some closing remarks.",
    ].join("\n");
    expect(extractNextPrompt(reply)).toBe(
      "Run the failing test in isolation and post output.",
    );
  });

  it("strips a fenced code block wrapper", () => {
    const reply = [
      "## NEXT PROMPT FOR CLAUDE CODE",
      "```",
      "Add a `--strict` flag that errors on unknown options.",
      "```",
    ].join("\n");
    expect(extractNextPrompt(reply)).toBe(
      "Add a `--strict` flag that errors on unknown options.",
    );
  });

  it("returns null when the heading is missing", () => {
    expect(
      extractNextPrompt(
        "Just a free-form review without the canonical heading.",
      ),
    ).toBeNull();
  });

  it("returns null on empty input", () => {
    expect(extractNextPrompt("")).toBeNull();
    expect(extractNextPrompt("   \n\n  ")).toBeNull();
  });
});

describe("copyTargetForReply", () => {
  it("returns the extracted prompt when present", () => {
    const reply = [
      "## NEXT PROMPT FOR CLAUDE CODE",
      "Do the thing.",
    ].join("\n");
    expect(copyTargetForReply(reply)).toBe("Do the thing.");
  });

  it("falls back to the full reply when no heading is present", () => {
    const full = "Some reviewer reply without a structured next prompt.";
    expect(copyTargetForReply(full)).toBe(full);
  });
});
