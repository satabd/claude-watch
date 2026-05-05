import { describe, expect, it } from "vitest";
import { computeSessionStats } from "@/lib/session-stats";
import type { SessionFull, TranscriptEvent } from "@/lib/api";

function evt(model: string, input: number, output: number): TranscriptEvent {
  return {
    type: "assistant",
    uuid: `u-${model}-${output}`,
    parent_uuid: null,
    timestamp: null,
    session_id: "s",
    cwd: null,
    git_branch: null,
    entrypoint: null,
    is_sidechain: false,
    role: "assistant",
    text_blocks: ["x"],
    thinking_blocks: [],
    tool_uses: [],
    tool_results: [],
    model,
    user_text: null,
    attribution_plugin: null,
    attribution_skill: null,
    pr: null,
    ai_title: null,
    is_command_artifact: false,
    usage: {
      input_tokens: input,
      output_tokens: output,
      cache_read_input_tokens: 0,
      cache_creation_input_tokens: 0,
    },
  };
}

function makeSession(events: TranscriptEvent[]): SessionFull {
  return {
    meta: {
      session_id: "s",
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
    },
    events,
  };
}

describe("computeSessionStats", () => {
  it("empty session → all zeros, dominantModel null, costIsFallback true", () => {
    const stats = computeSessionStats(makeSession([]));
    expect(stats.visibleTurns).toBe(0);
    expect(stats.inputTokens).toBe(0);
    expect(stats.outputTokens).toBe(0);
    expect(stats.cacheReadTokens).toBe(0);
    expect(stats.cacheCreateTokens).toBe(0);
    expect(stats.costEstimate).toBe(0);
    expect(stats.dominantModel).toBeNull();
    expect(stats.costIsFallback).toBe(true);
  });

  it("null session → empty stats", () => {
    const stats = computeSessionStats(null);
    expect(stats.dominantModel).toBeNull();
    expect(stats.models).toEqual([]);
  });

  it("mixed haiku + sonnet picks the higher-output model and computes cost", () => {
    const events = [
      evt("claude-haiku-4-5-20251001", 1000, 500),
      evt("claude-sonnet-4-6", 2000, 3000),
      evt("claude-sonnet-4-6", 1000, 1000),
    ];
    const stats = computeSessionStats(makeSession(events));

    expect(stats.inputTokens).toBe(4000);
    expect(stats.outputTokens).toBe(4500);
    // Sonnet has more total output (4000) than haiku (500)
    expect(stats.dominantModel).toBe("claude-sonnet-4-6");
    expect(stats.costIsFallback).toBe(false);

    // Cost using sonnet pricing: input $3/MTok, output $15/MTok
    const expected = (4000 * 3 + 4500 * 15) / 1_000_000;
    expect(stats.costEstimate).toBeCloseTo(expected, 6);
  });

  it("non-Anthropic model falls back to sonnet pricing and sets costIsFallback", () => {
    const events = [evt("gpt-5.5", 1000, 2000)];
    const stats = computeSessionStats(makeSession(events));
    expect(stats.dominantModel).toBe("gpt-5.5");
    expect(stats.costIsFallback).toBe(true);
    // Sonnet-fallback rate: 1000*3 + 2000*15 = 33000 / 1M
    expect(stats.costEstimate).toBeCloseTo(33000 / 1_000_000, 6);
  });
});
