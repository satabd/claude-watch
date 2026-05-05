import type { SessionFull, TranscriptEvent } from "@/lib/api";

// Rough $/MTok pricing for common Anthropic models. Cache read ~0.1× input.
// Non-Anthropic models won't show accurate cost; we fall back to sonnet rates.
const PRICING: Record<
  string,
  { in: number; out: number; cacheRead: number; cacheWrite: number }
> = {
  "claude-haiku-4-5-20251001": { in: 1, out: 5, cacheRead: 0.1, cacheWrite: 1.25 },
  "claude-sonnet-4-6": { in: 3, out: 15, cacheRead: 0.3, cacheWrite: 3.75 },
  "claude-opus-4-7": { in: 15, out: 75, cacheRead: 1.5, cacheWrite: 18.75 },
};

const FALLBACK_PRICING = PRICING["claude-sonnet-4-6"];

export interface ModelUsage {
  model: string;
  calls: number;
  outputTokens: number;
}

export interface SessionStats {
  visibleTurns: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheCreateTokens: number;
  cacheHitPct: number;
  costEstimate: number;
  models: ModelUsage[];
  dominantModel: string | null;
  /** Whether the cost estimate uses fallback pricing (non-Anthropic dominant). */
  costIsFallback: boolean;
}

export function computeSessionStats(session: SessionFull | null): SessionStats {
  const empty: SessionStats = {
    visibleTurns: 0,
    inputTokens: 0,
    outputTokens: 0,
    cacheReadTokens: 0,
    cacheCreateTokens: 0,
    cacheHitPct: 0,
    costEstimate: 0,
    models: [],
    dominantModel: null,
    costIsFallback: false,
  };
  if (!session) return empty;

  let inSum = 0;
  let outSum = 0;
  let cacheReadSum = 0;
  let cacheCreateSum = 0;
  const modelCounts: Record<string, ModelUsage> = {};
  let visibleTurns = 0;

  for (const ev of session.events) {
    if (isVisible(ev)) visibleTurns++;
    if (!ev.usage) continue;
    inSum += ev.usage.input_tokens;
    outSum += ev.usage.output_tokens;
    cacheReadSum += ev.usage.cache_read_input_tokens;
    cacheCreateSum += ev.usage.cache_creation_input_tokens;
    if (ev.model) {
      const m = (modelCounts[ev.model] ??= {
        model: ev.model,
        calls: 0,
        outputTokens: 0,
      });
      m.calls++;
      m.outputTokens += ev.usage.output_tokens;
    }
  }

  const totalIn = inSum + cacheReadSum + cacheCreateSum;
  const cacheHitPct = totalIn > 0 ? (cacheReadSum / totalIn) * 100 : 0;

  // Dominant model = the one with the most output tokens
  const sortedModels = Object.values(modelCounts).sort(
    (a, b) => b.outputTokens - a.outputTokens
  );
  const dominantModel = sortedModels[0]?.model ?? null;
  const pricing = (dominantModel && PRICING[dominantModel]) || FALLBACK_PRICING;
  const costEstimate =
    (inSum * pricing.in +
      outSum * pricing.out +
      cacheReadSum * pricing.cacheRead +
      cacheCreateSum * pricing.cacheWrite) /
    1_000_000;
  const costIsFallback = !dominantModel || !PRICING[dominantModel];

  return {
    visibleTurns,
    inputTokens: inSum,
    outputTokens: outSum,
    cacheReadTokens: cacheReadSum,
    cacheCreateTokens: cacheCreateSum,
    cacheHitPct,
    costEstimate,
    models: sortedModels,
    dominantModel,
    costIsFallback,
  };
}

function isVisible(ev: TranscriptEvent): boolean {
  if (ev.is_command_artifact) return false;
  if (ev.role === "user") return !!ev.user_text;
  if (ev.role === "assistant")
    return ev.text_blocks.length > 0 || ev.tool_uses.length > 0;
  return false;
}

export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return n.toLocaleString();
}

export function fmtCost(n: number): string {
  if (n === 0) return "—";
  if (n < 0.01) return "<$0.01";
  if (n < 1) return `$${n.toFixed(3)}`;
  if (n < 100) return `$${n.toFixed(2)}`;
  return `$${n.toFixed(0)}`;
}
