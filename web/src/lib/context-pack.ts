import type {
  ContextPackItem,
  PromptWriterContextMode,
  PromptWriterMode,
  SessionFull,
  TranscriptEvent,
} from "@/lib/api";

// ---- Per-mode total budgets (chars) ----
const MODE_BUDGETS: Record<PromptWriterContextMode, number> = {
  auto: 8000, // not used directly; auto resolves to a concrete mode
  none: 0,
  current_item: 3000,
  selected: 4000,
  recent_small: 6000,
  focused: 8000,
  expanded: 12000,
  full_session: 32000,

  // Legacy aliases — same budgets as their adaptive equivalents
  recent: 6000,
  selected_plus_nearby: 8000,
  summary: 12000,
  full_feature: 12000,
};

// Conservative per-message caps for heavy modes — assistant messages are usually
// the long ones, so they bear the brunt of trimming.
const PER_USER_MESSAGE_CAP = 2000;
const PER_ASSISTANT_MESSAGE_CAP = 1200;

// Continuation/reference phrases that should bump auto → recent_small
const CONTINUATION_PHRASES = [
  /\bprevious\b/i,
  /\babove\b/i,
  /\blast (message|reply|response|turn)\b/i,
  /\bcontinue\b/i,
  /\bsame idea\b/i,
  /\bwhat (we|you|they) (discussed|said|talked about)\b/i,
  /\bthat feature\b/i,
  /\bearlier\b/i,
];

const STRUCTURED_MODES = new Set<PromptWriterMode>([
  "developer_task",
  "bug_report",
  "design",
  "continue",
]);

export interface AutoDecision {
  mode: PromptWriterContextMode;
  reason: string;
}

export interface BuildOptions {
  session: SessionFull;
  contextMode: PromptWriterContextMode;
  selectedText?: string;
  sourceEventUuid?: string;
  summary?: string | null;
  /** Used by auto-resolution to enable continuation-phrase detection. */
  roughInput?: string;
  /** Used by auto-resolution. */
  writerMode?: PromptWriterMode;
}

// ---- Auto-mode decision ----

export function decideAutoMode(opts: {
  hasSelection: boolean;
  hasSourceEvent: boolean;
  roughInput: string;
  writerMode: PromptWriterMode;
}): AutoDecision {
  const { hasSelection, hasSourceEvent, roughInput, writerMode } = opts;
  const text = (roughInput || "").trim();

  // 1) Continuation phrase wins — user is asking us to use prior context.
  if (text && CONTINUATION_PHRASES.some((re) => re.test(text))) {
    return {
      mode: "recent_small",
      reason: "Recent context suggested: continuation phrase detected",
    };
  }

  const isStructured = STRUCTURED_MODES.has(writerMode);

  // 2) Structured modes get more context when there's a clear anchor.
  if (isStructured && (hasSourceEvent || hasSelection)) {
    return {
      mode: "focused",
      reason:
        "Focused context used: structured writer mode with " +
        (hasSourceEvent ? "source turn" : "selected text"),
    };
  }

  // 3) Selection beats source-only — selection is more specific.
  if (hasSelection) {
    return {
      mode: "selected",
      reason: "Selected text only: opened from selection",
    };
  }

  // 4) Source turn alone → just that turn.
  if (hasSourceEvent) {
    return {
      mode: "current_item",
      reason: "Current item only: opened from specific turn",
    };
  }

  // 5) Structured mode without an anchor still benefits from a tiny recent slice.
  if (isStructured) {
    return {
      mode: "recent_small",
      reason: "Recent context: structured writer mode",
    };
  }

  // 6) Default — no context. "Fix grammar", "make this professional", etc.
  return {
    mode: "none",
    reason: "No context used: simple rewrite",
  };
}

/** Resolve the (effective) mode if the user picked Auto. */
export function resolveContextMode(
  contextMode: PromptWriterContextMode,
  decision: AutoDecision
): { effective: PromptWriterContextMode; reason: string } {
  if (contextMode === "auto") {
    return { effective: decision.mode, reason: decision.reason };
  }
  // Normalize legacy aliases
  const normalized = normalizeLegacy(contextMode);
  return {
    effective: normalized,
    reason: contextMode === normalized ? "" : `Using ${normalized} (was ${contextMode})`,
  };
}

function normalizeLegacy(m: PromptWriterContextMode): PromptWriterContextMode {
  switch (m) {
    case "recent":
      return "recent_small";
    case "selected_plus_nearby":
      return "focused";
    case "summary":
    case "full_feature":
      return "expanded";
    default:
      return m;
  }
}

// ---- Trimming helpers ----

function trimToBudget(text: string, budget: number): { text: string; trimmed: boolean } {
  if (text.length <= budget) return { text, trimmed: false };
  if (budget <= 60) return { text: text.slice(0, Math.max(0, budget)), trimmed: true };
  // Keep head + tail with elision marker
  const head = text.slice(0, Math.floor(budget * 0.65));
  const tail = text.slice(-Math.floor(budget * 0.25));
  const elided = text.length - head.length - tail.length;
  const marker = `\n…[${elided.toLocaleString()} chars elided]…\n`;
  // If the marker pushed us past budget, shave more from head
  let combined = head + marker + tail;
  if (combined.length > budget) {
    const overflow = combined.length - budget;
    combined = head.slice(0, head.length - overflow - marker.length) + marker + tail;
  }
  return { text: combined, trimmed: true };
}

function turnText(ev: TranscriptEvent): string {
  if (ev.role === "assistant") {
    const text = ev.text_blocks.join("\n\n").trim();
    if (text) return text;
    if (ev.tool_uses.length) {
      const tools = ev.tool_uses.map((t) => {
        const i = t.input ?? {};
        if (t.name === "Bash" || t.name === "PowerShell") {
          return `${t.name}: ${String(i.command ?? "").split("\n")[0].slice(0, 120)}`;
        }
        if (
          t.name === "Read" ||
          t.name === "Edit" ||
          t.name === "Write" ||
          t.name === "MultiEdit"
        ) {
          return `${t.name}: ${i.file_path ?? ""}`;
        }
        if (t.name === "Grep" || t.name === "Glob") {
          return `${t.name}: ${i.pattern ?? i.glob ?? ""}`;
        }
        return t.name;
      });
      return `[tool-only turn] ${tools.join(" | ")}`;
    }
    return "";
  }
  if (ev.role === "user") return ev.user_text || "";
  return "";
}

function turnLabel(ev: TranscriptEvent, idx: number): string {
  const role = ev.role === "user" ? "You" : "Claude";
  const time = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : "";
  return `${role}${time ? ` (${time})` : ""} #${idx}`;
}

function isToolOnly(ev: TranscriptEvent): boolean {
  return (
    ev.role === "assistant" &&
    !ev.text_blocks.some((t) => t.trim()) &&
    ev.tool_uses.length > 0
  );
}

/** Visible, prompt-relevant turns: include user, assistant-with-text, and
 * (only when forced) tool-only assistant turns. */
function relevantTurns(
  events: TranscriptEvent[],
  includeToolOnlyForUuids: Set<string>
): TranscriptEvent[] {
  return events.filter((e) => {
    if (e.is_command_artifact) return false;
    if (e.role === "user") return !!e.user_text;
    if (e.role === "assistant") {
      if (e.text_blocks.some((t) => t.trim())) return true;
      if (isToolOnly(e) && includeToolOnlyForUuids.has(e.uuid)) return true;
      return false;
    }
    return false;
  });
}

// ---- Pack builder ----

export function buildContextPack(opts: BuildOptions): ContextPackItem[] {
  const { session, selectedText, sourceEventUuid, summary, roughInput, writerMode } = opts;

  // Resolve auto if needed
  let mode = opts.contextMode;
  if (mode === "auto") {
    const decision = decideAutoMode({
      hasSelection: !!selectedText?.trim(),
      hasSourceEvent: !!sourceEventUuid,
      roughInput: roughInput ?? "",
      writerMode: writerMode ?? "improve",
    });
    mode = decision.mode;
  } else {
    mode = normalizeLegacy(mode);
  }

  // none → nothing
  if (mode === "none") return [];

  const budget = MODE_BUDGETS[mode] ?? 6000;
  const out: ContextPackItem[] = [];
  let used = 0;

  // Tool-only inclusion: only the source turn (if it's tool-only) is allowed in
  const includeToolOnlyForUuids = new Set<string>();
  if (sourceEventUuid) includeToolOnlyForUuids.add(sourceEventUuid);

  const allTurns = relevantTurns(session.events, includeToolOnlyForUuids);
  const sourceIdx = sourceEventUuid
    ? allTurns.findIndex((e) => e.uuid === sourceEventUuid)
    : -1;

  // Always include lightweight project + session metadata when we have any budget.
  // (Skip for selected mode to keep it minimal.)
  const wantMeta =
    mode !== "selected" &&
    mode !== "current_item" &&
    !!session.meta.cwd;
  if (wantMeta && session.meta.cwd) {
    const meta = `Working dir: ${session.meta.cwd}${
      session.meta.git_branch ? `\nGit branch: ${session.meta.git_branch}` : ""
    }`;
    out.push({ id: "project", kind: "project", label: "Project", text: meta });
    used += meta.length;
  }

  // Selected text
  if (selectedText?.trim() && (mode === "selected" || mode === "focused")) {
    const remaining = budget - used;
    const { text, trimmed } = trimToBudget(selectedText.trim(), Math.max(0, remaining));
    if (text) {
      out.push({
        id: "selected",
        kind: "selected",
        label: trimmed ? "Selected text (trimmed)" : "Selected text",
        text,
      });
      used += text.length;
    }
  }

  switch (mode) {
    case "selected":
      // Selected text only — already added above.
      break;

    case "current_item": {
      const ev = sourceIdx >= 0 ? allTurns[sourceIdx] : null;
      if (ev) {
        const cap = ev.role === "user" ? PER_USER_MESSAGE_CAP : PER_ASSISTANT_MESSAGE_CAP;
        const remaining = Math.min(cap, budget - used);
        const { text, trimmed } = trimToBudget(turnText(ev), Math.max(0, remaining));
        if (text) {
          out.push({
            id: `src-${ev.uuid}`,
            kind: "message",
            label: `${turnLabel(ev, sourceIdx + 1)} (source)${trimmed ? " · trimmed" : ""}`,
            text,
          });
          used += text.length;
        }
      }
      break;
    }

    case "recent_small": {
      const slice = allTurns.slice(-3);
      addMessageSlice(out, slice, allTurns.length - slice.length, budget - used);
      break;
    }

    case "focused": {
      // selected/source + 2 before + 2 after
      let center = sourceIdx;
      if (center < 0) {
        // No source — fall back to recent_small-style window
        const slice = allTurns.slice(-5);
        addMessageSlice(out, slice, allTurns.length - slice.length, budget - used);
        break;
      }
      const start = Math.max(0, center - 2);
      const end = Math.min(allTurns.length, center + 3);
      const slice = allTurns.slice(start, end);
      addMessageSlice(out, slice, start, budget - used, sourceEventUuid);
      break;
    }

    case "expanded": {
      // Optional summary + last ~6 messages
      const summaryBudget = Math.min(4000, Math.floor((budget - used) * 0.35));
      if (summary && summaryBudget > 200) {
        const { text, trimmed } = trimToBudget(summary, summaryBudget);
        out.push({
          id: "summary",
          kind: "summary",
          label: trimmed ? "Session summary (trimmed)" : "Session summary",
          text,
        });
        used += text.length;
      }
      const slice = allTurns.slice(-6);
      addMessageSlice(out, slice, allTurns.length - slice.length, budget - used);
      break;
    }

    case "full_session": {
      // Full session — heavily trimmed per-message
      addMessageSlice(out, allTurns, 0, budget - used, sourceEventUuid, /* tighter */ true);
      break;
    }
  }

  return enforceTotalCap(out, budget);
}

function addMessageSlice(
  out: ContextPackItem[],
  slice: TranscriptEvent[],
  startIdx: number,
  remainingBudget: number,
  highlightUuid?: string,
  aggressive = false
): void {
  if (remainingBudget <= 0 || slice.length === 0) return;

  // Two-pass: user messages first (preferred), then assistant.
  const indexed = slice.map((ev, i) => ({ ev, idx: startIdx + i }));
  // Sort: user before assistant, preserving original order otherwise
  const ordered = [
    ...indexed.filter((x) => x.ev.role === "user"),
    ...indexed.filter((x) => x.ev.role !== "user"),
  ];

  // Compute per-message cap dynamically.
  const userCap = aggressive ? 800 : PER_USER_MESSAGE_CAP;
  const asstCap = aggressive ? 600 : PER_ASSISTANT_MESSAGE_CAP;

  const queued: { id: string; kind: ContextPackItem["kind"]; label: string; text: string; idx: number; }[] = [];

  let used = 0;
  for (const { ev, idx } of ordered) {
    const cap = ev.role === "user" ? userCap : asstCap;
    const slot = Math.min(cap, remainingBudget - used);
    if (slot <= 100) continue;
    const raw = turnText(ev);
    if (!raw) continue;
    const { text, trimmed } = trimToBudget(raw, slot);
    const label = `${turnLabel(ev, idx + 1)}${
      ev.uuid === highlightUuid ? " (source)" : ""
    }${trimmed ? " · trimmed" : ""}`;
    queued.push({
      id: `msg-${ev.uuid}`,
      kind: "message",
      label,
      text,
      idx,
    });
    used += text.length;
    if (used >= remainingBudget) break;
  }

  // Re-emit in original order so the conversation reads naturally.
  queued.sort((a, b) => a.idx - b.idx);
  for (const q of queued) {
    out.push({ id: q.id, kind: q.kind, label: q.label, text: q.text });
  }
}

function enforceTotalCap(items: ContextPackItem[], cap: number): ContextPackItem[] {
  let total = 0;
  const out: ContextPackItem[] = [];
  for (const it of items) {
    const remaining = cap - total;
    if (remaining <= 100) break;
    if (it.text.length <= remaining) {
      out.push(it);
      total += it.text.length;
    } else {
      const { text } = trimToBudget(it.text, remaining);
      out.push({ ...it, label: it.label.includes("trimmed") ? it.label : `${it.label} · trimmed`, text });
      break;
    }
  }
  return out;
}

export function packCharCount(items: ContextPackItem[]): number {
  return items.reduce((s, it) => s + it.text.length, 0);
}

export function estimateTokens(chars: number): number {
  return Math.ceil(chars / 4);
}

/** Pick a sensible default — used when opening the writer (now always 'auto'). */
export function defaultContextMode(): PromptWriterContextMode {
  return "auto";
}

/** True when the mode requires explicit user confirmation before sending. */
export function modeRequiresConfirmation(mode: PromptWriterContextMode): boolean {
  return mode === "full_session";
}

/** True when the pack is large enough to warn the user. */
export const LARGE_CONTEXT_THRESHOLD = 8000;
