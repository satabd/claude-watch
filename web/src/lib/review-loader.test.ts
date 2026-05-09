import { describe, expect, it } from "vitest";
import type { ReviewMessage, ReviewThread } from "./api";
import { combineThreadMessages } from "./review-loader";
import { EMPTY_GROUPED_REVIEW_MESSAGES } from "./review-grouping";

function mkThread(p: Partial<ReviewThread>): ReviewThread {
  return {
    id: 0,
    name: "t",
    provider: "codex",
    project_bucket: "bucket",
    claude_session_id: "S",
    provider_session_id: null,
    active_skill_id: null,
    provider_session_skill_id: null,
    provider_session_skill_version: null,
    created_at: 0,
    updated_at: 0,
    archived_at: null,
    ...p,
  };
}

function mkMsg(p: Partial<ReviewMessage>): ReviewMessage {
  return {
    id: 0,
    thread_id: 1,
    role: "reviewer",
    content: "",
    source_session_id: "S",
    source_turn_uuid: null,
    context_used_json: null,
    evidence_used_json: null,
    provider: "codex",
    model: null,
    estimated_tokens: null,
    provider_tokens: null,
    created_at: 0,
    ...p,
  };
}

describe("combineThreadMessages", () => {
  it("returns the stable empty result for an empty thread list", () => {
    const result = combineThreadMessages([], "S", {});
    expect(result).toBe(EMPTY_GROUPED_REVIEW_MESSAGES);
  });

  it("returns the stable empty result when no thread matches the session", () => {
    const threads = [
      mkThread({ id: 1, claude_session_id: "OTHER" }),
      mkThread({ id: 2, claude_session_id: "ANOTHER" }),
    ];
    const messagesByThread = {
      1: [mkMsg({ id: 100, source_turn_uuid: "T1" })],
    };
    const result = combineThreadMessages(threads, "S", messagesByThread);
    expect(result).toBe(EMPTY_GROUPED_REVIEW_MESSAGES);
  });

  it("excludes archived threads even when their claude_session_id matches", () => {
    const threads = [
      mkThread({ id: 1, claude_session_id: "S", archived_at: 999 }),
      mkThread({ id: 2, claude_session_id: "S" }),
    ];
    const messagesByThread = {
      1: [mkMsg({ id: 100, source_turn_uuid: "T-archived" })],
      2: [mkMsg({ id: 200, source_turn_uuid: "T-active" })],
    };
    const result = combineThreadMessages(threads, "S", messagesByThread);
    expect(result.byTurn.has("T-archived")).toBe(false);
    expect(result.byTurn.has("T-active")).toBe(true);
    expect(result.byTurn.get("T-active")?.[0].id).toBe(200);
  });

  it("filters out other-session threads while keeping matching ones", () => {
    const threads = [
      mkThread({ id: 1, claude_session_id: "S" }),
      mkThread({ id: 2, claude_session_id: "OTHER" }),
    ];
    const messagesByThread = {
      1: [mkMsg({ id: 1, source_turn_uuid: "T1" })],
      2: [mkMsg({ id: 99, source_turn_uuid: "T1" })],
    };
    const result = combineThreadMessages(threads, "S", messagesByThread);
    const list = result.byTurn.get("T1") ?? [];
    // Only thread 1's message survives the session filter.
    expect(list.map((m) => m.id)).toEqual([1]);
  });

  it("merges messages from multiple matching threads anchored to the same turn", () => {
    const threads = [
      mkThread({ id: 1, claude_session_id: "S", updated_at: 100 }),
      mkThread({ id: 2, claude_session_id: "S", updated_at: 200 }),
    ];
    const messagesByThread = {
      1: [mkMsg({ id: 10, thread_id: 1, source_turn_uuid: "T1", created_at: 1 })],
      2: [mkMsg({ id: 20, thread_id: 2, source_turn_uuid: "T1", created_at: 2 })],
    };
    const result = combineThreadMessages(threads, "S", messagesByThread);
    const list = result.byTurn.get("T1") ?? [];
    expect(list).toHaveLength(2);
    // Chronological order across threads — the underlying grouping
    // sorts by created_at, not thread id.
    expect(list.map((m) => m.id)).toEqual([10, 20]);
    expect(list.map((m) => m.thread_id)).toEqual([1, 2]);
  });

  it("missing messagesByThread entry for a matching thread is empty, not a throw", () => {
    const threads = [mkThread({ id: 1, claude_session_id: "S" })];
    // Caller didn't fetch messages for thread 1 — we must not crash.
    const result = combineThreadMessages(threads, "S", {});
    expect(result).toBe(EMPTY_GROUPED_REVIEW_MESSAGES);
  });

  it("preserves null-anchored messages in the noAnchor bucket", () => {
    const threads = [mkThread({ id: 1, claude_session_id: "S" })];
    const messagesByThread = {
      1: [
        mkMsg({ id: 1, source_turn_uuid: "T1", created_at: 1 }),
        mkMsg({ id: 2, source_turn_uuid: null, created_at: 2 }),
      ],
    };
    const result = combineThreadMessages(threads, "S", messagesByThread);
    expect(result.byTurn.get("T1")?.map((m) => m.id)).toEqual([1]);
    expect(result.noAnchor.map((m) => m.id)).toEqual([2]);
  });
});
