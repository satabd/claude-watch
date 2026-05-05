import { describe, expect, it } from "vitest";
import { sessionDisplayName } from "@/lib/session-display";
import type { SessionMeta } from "@/lib/api";

function makeMeta(overrides: Partial<SessionMeta> = {}): SessionMeta {
  return {
    session_id: "abcdef0123456789",
    bucket: "b",
    file_path: "/tmp/x.jsonl",
    size_bytes: 0,
    line_count: null,
    modified_ms: 0,
    cwd: null,
    git_branch: null,
    entrypoint: null,
    version: null,
    ai_title: null,
    first_prompt: null,
    last_model: null,
    remote_name: null,
    ...overrides,
  };
}

describe("sessionDisplayName", () => {
  it("prefers ai_title", () => {
    const m = makeMeta({ ai_title: "Big Refactor", first_prompt: "fix the thing" });
    expect(sessionDisplayName(m)).toBe("Big Refactor");
  });

  it("falls back to first_prompt when no ai_title", () => {
    const m = makeMeta({ first_prompt: "Add tests for parser" });
    expect(sessionDisplayName(m)).toBe("Add tests for parser");
  });

  it("falls back to 'Session <id8>' when both are null", () => {
    const m = makeMeta();
    expect(sessionDisplayName(m)).toBe("Session abcdef01");
  });

  it("collapses multiline first_prompt to single line", () => {
    const m = makeMeta({ first_prompt: "first line\n\nsecond line\n  third" });
    expect(sessionDisplayName(m)).toBe("first line second line third");
  });

  it("honors max length / truncation", () => {
    const long = "a".repeat(80);
    const m = makeMeta({ ai_title: long });
    const out = sessionDisplayName(m, 30);
    expect(out.length).toBeLessThanOrEqual(30);
    expect(out.endsWith("…")).toBe(true);
  });
});
