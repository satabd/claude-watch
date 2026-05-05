import { describe, expect, it } from "vitest";
import {
  buildContextPack,
  decideAutoMode,
  resolveContextMode,
} from "@/lib/context-pack";
import type { SessionFull, TranscriptEvent } from "@/lib/api";

// ---- decideAutoMode ----

describe("decideAutoMode", () => {
  it("continuation phrase wins → recent_small", () => {
    const d = decideAutoMode({
      hasSelection: true,
      hasSourceEvent: true,
      roughInput: "continue from above",
      writerMode: "developer_task",
    });
    expect(d.mode).toBe("recent_small");
  });

  it("structured mode + source event → focused", () => {
    const d = decideAutoMode({
      hasSelection: false,
      hasSourceEvent: true,
      roughInput: "",
      writerMode: "developer_task",
    });
    expect(d.mode).toBe("focused");
  });

  it("has selection (non-structured mode) → selected", () => {
    const d = decideAutoMode({
      hasSelection: true,
      hasSourceEvent: false,
      roughInput: "",
      writerMode: "improve",
    });
    expect(d.mode).toBe("selected");
  });

  it("source event only (non-structured) → current_item", () => {
    const d = decideAutoMode({
      hasSelection: false,
      hasSourceEvent: true,
      roughInput: "",
      writerMode: "improve",
    });
    expect(d.mode).toBe("current_item");
  });

  it("structured mode without anchor → recent_small", () => {
    const d = decideAutoMode({
      hasSelection: false,
      hasSourceEvent: false,
      roughInput: "",
      writerMode: "design",
    });
    expect(d.mode).toBe("recent_small");
  });

  it("default (no anchor, no continuation, simple mode) → none", () => {
    const d = decideAutoMode({
      hasSelection: false,
      hasSourceEvent: false,
      roughInput: "fix grammar",
      writerMode: "improve",
    });
    expect(d.mode).toBe("none");
  });
});

// ---- resolveContextMode ----

describe("resolveContextMode", () => {
  const decision = { mode: "focused" as const, reason: "auto reason" };

  it("auto resolves to the decision's mode", () => {
    const r = resolveContextMode("auto", decision);
    expect(r.effective).toBe("focused");
    expect(r.reason).toBe("auto reason");
  });

  it("legacy 'recent' normalizes to 'recent_small'", () => {
    const r = resolveContextMode("recent", decision);
    expect(r.effective).toBe("recent_small");
  });

  it("legacy 'selected_plus_nearby' normalizes to 'focused'", () => {
    const r = resolveContextMode("selected_plus_nearby", decision);
    expect(r.effective).toBe("focused");
  });

  it("non-legacy mode passes through unchanged", () => {
    const r = resolveContextMode("expanded", decision);
    expect(r.effective).toBe("expanded");
    expect(r.reason).toBe("");
  });
});

// ---- buildContextPack ----

function userTurn(uuid: string, text: string): TranscriptEvent {
  return {
    type: "user",
    uuid,
    parent_uuid: null,
    timestamp: "2025-01-01T00:00:00Z",
    session_id: "s1",
    cwd: "/x",
    git_branch: null,
    entrypoint: null,
    is_sidechain: false,
    role: "user",
    text_blocks: [],
    thinking_blocks: [],
    tool_uses: [],
    tool_results: [],
    model: null,
    user_text: text,
    attribution_plugin: null,
    attribution_skill: null,
    pr: null,
    ai_title: null,
    is_command_artifact: false,
    usage: null,
  };
}

function assistantTurn(uuid: string, text: string): TranscriptEvent {
  return {
    ...userTurn(uuid, ""),
    role: "assistant",
    user_text: null,
    text_blocks: [text],
    model: "claude-sonnet-4-6",
  };
}

function makeSession(events: TranscriptEvent[], cwd = "/proj"): SessionFull {
  return {
    meta: {
      session_id: "sess",
      bucket: "b",
      file_path: "/tmp/x.jsonl",
      size_bytes: 0,
      line_count: null,
      modified_ms: 0,
      cwd,
      git_branch: "main",
      entrypoint: null,
      version: null,
      ai_title: null,
      first_prompt: null,
      last_model: null,
      remote_name: null,
    },
    events,
  };
}

describe("buildContextPack", () => {
  it("'none' mode returns an empty array", () => {
    const session = makeSession([userTurn("u-1", "hi")]);
    const pack = buildContextPack({ session, contextMode: "none" });
    expect(pack).toEqual([]);
  });

  it("respects per-mode budgets (recent_small adds project meta + recent slice)", () => {
    const events = [
      userTurn("u-1", "first"),
      assistantTurn("a-1", "first reply"),
      userTurn("u-2", "second"),
      assistantTurn("a-2", "second reply"),
      userTurn("u-3", "third"),
    ];
    const session = makeSession(events);
    const pack = buildContextPack({ session, contextMode: "recent_small" });
    // Should include project meta + at least one message item
    expect(pack.some((p) => p.kind === "project")).toBe(true);
    expect(pack.some((p) => p.kind === "message")).toBe(true);
    const totalChars = pack.reduce((s, it) => s + it.text.length, 0);
    expect(totalChars).toBeLessThanOrEqual(6000);
  });

  it("trims items when over per-message cap", () => {
    const long = "x".repeat(5000);
    const events = [userTurn("u-1", long), assistantTurn("a-1", "ok")];
    const session = makeSession(events);
    const pack = buildContextPack({ session, contextMode: "recent_small" });
    const userItem = pack.find((p) => p.label.startsWith("You"));
    // The user message had 5000 chars; PER_USER_MESSAGE_CAP is 2000 — must be trimmed
    expect(userItem).toBeDefined();
    expect(userItem!.text.length).toBeLessThanOrEqual(2000);
    expect(userItem!.label).toMatch(/trimmed/);
  });

  it("enforceTotalCap ratchets when total would exceed budget", () => {
    // full_session has 32k budget; flood with many large messages
    const events: TranscriptEvent[] = [];
    for (let i = 0; i < 40; i++) {
      events.push(userTurn(`u-${i}`, "U" + "y".repeat(2000)));
      events.push(assistantTurn(`a-${i}`, "A" + "z".repeat(1500)));
    }
    const session = makeSession(events);
    const pack = buildContextPack({ session, contextMode: "full_session" });
    const total = pack.reduce((s, it) => s + it.text.length, 0);
    expect(total).toBeLessThanOrEqual(32000);
  });
});
