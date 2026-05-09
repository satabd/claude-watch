import { describe, expect, it } from "vitest";
import type { ReviewMessage } from "./api";
import {
  EMPTY_GROUPED_REVIEW_MESSAGES,
  findLatestPromptForTurn,
  getMessagesForTurn,
  groupReviewMessagesByTurn,
} from "./review-grouping";

/** Build a ReviewMessage with sensible defaults. Override any field via
 *  the partial. The grouping helpers care about role, content,
 *  source_turn_uuid, created_at, and (for the prompt extraction) the
 *  context_used_json.skill_id field — everything else is irrelevant. */
function mkMsg(p: Partial<ReviewMessage>): ReviewMessage {
  return {
    id: 0,
    thread_id: 1,
    role: "reviewer",
    content: "",
    source_session_id: "sess",
    source_turn_uuid: null,
    context_used_json: { skill_id: "quick_review" },
    evidence_used_json: null,
    provider: "codex",
    model: null,
    estimated_tokens: null,
    provider_tokens: null,
    created_at: 0,
    ...p,
  };
}

const QUICK_REVIEW_BODY = `VERDICT:
Looks fine.

WHY:
- one
- two

NEXT ACTION:
Ship it.

PROMPT TO SEND CLAUDE:
Run the tests and report.
`;

const COACH_BODY = `CLARIFIED INTENT:
Refactor parser.

PROMPT TO SEND CLAUDE:
Refactor parser.py to dedupe helpers.

WHY THIS WORKS:
- specific
- preserves invariants
`;

// ---------------------------------------------------------------------------
// groupReviewMessagesByTurn
// ---------------------------------------------------------------------------

describe("groupReviewMessagesByTurn", () => {
  it("returns the stable empty result for an empty input", () => {
    expect(groupReviewMessagesByTurn([])).toBe(EMPTY_GROUPED_REVIEW_MESSAGES);
    // Identity check matters — React consumers depend on stable refs.
    expect(groupReviewMessagesByTurn([]).byTurn).toBe(
      EMPTY_GROUPED_REVIEW_MESSAGES.byTurn,
    );
  });

  it("buckets messages by source_turn_uuid", () => {
    const msgs = [
      mkMsg({ id: 1, source_turn_uuid: "T1", created_at: 100 }),
      mkMsg({ id: 2, source_turn_uuid: "T2", created_at: 200 }),
      mkMsg({ id: 3, source_turn_uuid: "T1", created_at: 300 }),
    ];
    const g = groupReviewMessagesByTurn(msgs);
    expect(g.count).toBe(2);
    expect(g.byTurn.get("T1")?.map((m) => m.id)).toEqual([1, 3]);
    expect(g.byTurn.get("T2")?.map((m) => m.id)).toEqual([2]);
  });

  it("excludes null source_turn_uuid messages from turn groups", () => {
    const msgs = [
      mkMsg({ id: 1, source_turn_uuid: "T1", created_at: 1 }),
      mkMsg({ id: 2, source_turn_uuid: null, created_at: 2 }),
      mkMsg({ id: 3, source_turn_uuid: null, created_at: 3 }),
    ];
    const g = groupReviewMessagesByTurn(msgs);
    expect(g.byTurn.size).toBe(1);
    expect(g.byTurn.get("T1")?.map((m) => m.id)).toEqual([1]);
    expect(g.noAnchor.map((m) => m.id)).toEqual([2, 3]);
    // null is never a key in byTurn — even an explicit lookup misses.
    expect(g.byTurn.has("")).toBe(false);
  });

  it("sorts each per-turn list chronologically (ascending) by created_at", () => {
    const msgs = [
      mkMsg({ id: 3, source_turn_uuid: "T1", created_at: 300 }),
      mkMsg({ id: 1, source_turn_uuid: "T1", created_at: 100 }),
      mkMsg({ id: 2, source_turn_uuid: "T1", created_at: 200 }),
    ];
    const g = groupReviewMessagesByTurn(msgs);
    expect(g.byTurn.get("T1")?.map((m) => m.created_at)).toEqual([100, 200, 300]);
  });

  it("merges messages from multiple threads anchored to the same turn", () => {
    // Two different thread_ids both touching turn T1. The loader passes
    // them concatenated; the grouping must NOT split by thread.
    const msgs = [
      mkMsg({ id: 1, thread_id: 7, source_turn_uuid: "T1", created_at: 100 }),
      mkMsg({ id: 2, thread_id: 9, source_turn_uuid: "T1", created_at: 200 }),
      mkMsg({ id: 3, thread_id: 7, source_turn_uuid: "T1", created_at: 300 }),
    ];
    const g = groupReviewMessagesByTurn(msgs);
    const list = g.byTurn.get("T1") ?? [];
    expect(list).toHaveLength(3);
    expect(list.map((m) => m.id)).toEqual([1, 2, 3]);
    // Threads are preserved on each message — the grouping doesn't lose
    // provenance, it just doesn't partition by it.
    expect(list.map((m) => m.thread_id)).toEqual([7, 9, 7]);
  });

  it("sorts the noAnchor bucket chronologically too", () => {
    const msgs = [
      mkMsg({ id: 3, source_turn_uuid: null, created_at: 300 }),
      mkMsg({ id: 1, source_turn_uuid: null, created_at: 100 }),
    ];
    const g = groupReviewMessagesByTurn(msgs);
    expect(g.noAnchor.map((m) => m.id)).toEqual([1, 3]);
  });
});

// ---------------------------------------------------------------------------
// getMessagesForTurn
// ---------------------------------------------------------------------------

describe("getMessagesForTurn", () => {
  it("returns messages for the requested turn", () => {
    const g = groupReviewMessagesByTurn([
      mkMsg({ id: 1, source_turn_uuid: "T1" }),
      mkMsg({ id: 2, source_turn_uuid: "T2" }),
    ]);
    expect(getMessagesForTurn(g, "T1").map((m) => m.id)).toEqual([1]);
  });

  it("returns an empty array for an unknown turn (and the SAME ref each call)", () => {
    const g = groupReviewMessagesByTurn([
      mkMsg({ id: 1, source_turn_uuid: "T1" }),
    ]);
    const a = getMessagesForTurn(g, "T-missing");
    const b = getMessagesForTurn(g, "T-other");
    expect(a).toEqual([]);
    expect(b).toEqual([]);
    // Stable empty array reference — useful as a useMemo / useEffect dep.
    expect(a).toBe(b);
  });

  it("returns empty for null/undefined uuid (don't accidentally match noAnchor)", () => {
    const g = groupReviewMessagesByTurn([
      mkMsg({ id: 1, source_turn_uuid: null }),
    ]);
    expect(getMessagesForTurn(g, null)).toEqual([]);
    expect(getMessagesForTurn(g, undefined)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// findLatestPromptForTurn
// ---------------------------------------------------------------------------

describe("findLatestPromptForTurn", () => {
  it("returns the most recent reviewer message's prompt for a turn", () => {
    const g = groupReviewMessagesByTurn([
      mkMsg({
        id: 1,
        role: "user",
        content: "ask",
        source_turn_uuid: "T1",
        created_at: 100,
      }),
      mkMsg({
        id: 2,
        role: "reviewer",
        content: QUICK_REVIEW_BODY,
        source_turn_uuid: "T1",
        created_at: 200,
      }),
    ]);
    const found = findLatestPromptForTurn(g, "T1");
    expect(found).not.toBeNull();
    expect(found!.msg.id).toBe(2);
    expect(found!.prompt).toBe("Run the tests and report.");
    expect(found!.renderMode).toBe("critical_or_quick");
  });

  it("walks newest-first and returns the most recent valid prompt", () => {
    // Two reviewer messages on the same turn — the LATER one wins.
    const g = groupReviewMessagesByTurn([
      mkMsg({
        id: 10,
        role: "reviewer",
        content: QUICK_REVIEW_BODY,
        source_turn_uuid: "T1",
        created_at: 100,
      }),
      mkMsg({
        id: 11,
        role: "reviewer",
        content:
          "VERDICT:\nNew take.\n\nPROMPT TO SEND CLAUDE:\nDo the new thing.\n",
        source_turn_uuid: "T1",
        created_at: 200,
      }),
    ]);
    const found = findLatestPromptForTurn(g, "T1");
    expect(found!.msg.id).toBe(11);
    expect(found!.prompt).toBe("Do the new thing.");
  });

  it("uses the coach parser for prompt_coach messages", () => {
    const g = groupReviewMessagesByTurn([
      mkMsg({
        id: 1,
        role: "reviewer",
        content: COACH_BODY,
        source_turn_uuid: "T1",
        context_used_json: { skill_id: "prompt_coach" },
      }),
    ]);
    const found = findLatestPromptForTurn(g, "T1");
    expect(found).not.toBeNull();
    expect(found!.prompt).toBe("Refactor parser.py to dedupe helpers.");
    expect(found!.renderMode).toBe("prompt_coach");
  });

  it("returns null when only user messages anchor to the turn", () => {
    const g = groupReviewMessagesByTurn([
      mkMsg({
        id: 1,
        role: "user",
        content: "thinking out loud",
        source_turn_uuid: "T1",
      }),
    ]);
    expect(findLatestPromptForTurn(g, "T1")).toBeNull();
  });

  it("returns null when reviewer messages exist but none has a prompt section", () => {
    const g = groupReviewMessagesByTurn([
      mkMsg({
        id: 1,
        role: "reviewer",
        content: "Free-form ramble. No headings.",
        source_turn_uuid: "T1",
      }),
    ]);
    expect(findLatestPromptForTurn(g, "T1")).toBeNull();
  });

  it("returns null for an unknown turn", () => {
    const g = groupReviewMessagesByTurn([
      mkMsg({
        id: 1,
        role: "reviewer",
        content: QUICK_REVIEW_BODY,
        source_turn_uuid: "T1",
      }),
    ]);
    expect(findLatestPromptForTurn(g, "T-missing")).toBeNull();
  });

  it("returns null for null/undefined uuid", () => {
    const g = EMPTY_GROUPED_REVIEW_MESSAGES;
    expect(findLatestPromptForTurn(g, null)).toBeNull();
    expect(findLatestPromptForTurn(g, undefined)).toBeNull();
  });
});
