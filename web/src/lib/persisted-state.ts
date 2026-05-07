/** localStorage persistence for a small subset of the app store.
 *
 *  Only stable UI preferences are persisted — never transient model output,
 *  in-flight fetches, or anything already owned by the backend (e.g. provider
 *  settings live in SQLite, not here). All reads go through `loadPersisted()`
 *  which validates each field; unknown / mistyped values are silently dropped
 *  so a corrupted blob can never crash the app.
 *
 *  Persistence is bumped via `STORAGE_KEY` whenever the schema changes — old
 *  blobs are then ignored and the user starts fresh.
 */
import type { PromptWriterMode } from "./api";

const STORAGE_KEY = "watcher.ui.v1";
const WRITE_DEBOUNCE_MS = 200;

const VALID_PROMPT_MODES: ReadonlySet<PromptWriterMode> = new Set<PromptWriterMode>([
  "improve",
  "clarify",
  "developer_task",
  "bug_report",
  "design",
  "critical_review",
  "short_command",
  "continue",
]);

export interface Persisted {
  /** Last bucket the user clicked. May be stale (project removed) — caller
   *  must validate against current projects after they load. */
  selectedBucket: string | null;
  /** Last session_id within `selectedBucket`. Same staleness caveat. */
  selectedSessionId: string | null;
  scratchpadOpen: boolean;
  /** bucket -> open. Missing key falls back to the default heuristic
   *  (top-level open, child closed unless containing the active session). */
  expandedNodes: Record<string, boolean>;
  /** Prompt Writer mode. NOTE: contextMode is intentionally NOT persisted —
   *  it always resets to "auto" on open so a stale "full_session" choice
   *  can't accidentally pull a huge / expensive context next time. */
  promptWriterMode: PromptWriterMode;
}

/** Read + validate the persisted blob. Returns a partial — only fields whose
 *  shape passed validation are present. Safe to call on first run. */
export function loadPersisted(): Partial<Persisted> {
  if (typeof localStorage === "undefined") return {};
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(STORAGE_KEY);
  } catch {
    return {};
  }
  if (!raw) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {};
  }
  if (!parsed || typeof parsed !== "object") return {};
  const v = parsed as Record<string, unknown>;
  const out: Partial<Persisted> = {};

  if (v.selectedBucket === null || typeof v.selectedBucket === "string") {
    out.selectedBucket = v.selectedBucket as string | null;
  }
  if (v.selectedSessionId === null || typeof v.selectedSessionId === "string") {
    out.selectedSessionId = v.selectedSessionId as string | null;
  }
  if (typeof v.scratchpadOpen === "boolean") {
    out.scratchpadOpen = v.scratchpadOpen;
  }
  if (v.expandedNodes && typeof v.expandedNodes === "object") {
    const en: Record<string, boolean> = {};
    for (const [k, val] of Object.entries(v.expandedNodes as Record<string, unknown>)) {
      if (typeof val === "boolean") en[k] = val;
    }
    out.expandedNodes = en;
  }
  if (
    typeof v.promptWriterMode === "string" &&
    VALID_PROMPT_MODES.has(v.promptWriterMode as PromptWriterMode)
  ) {
    out.promptWriterMode = v.promptWriterMode as PromptWriterMode;
  }

  // selectedBucket and selectedSessionId must travel as a pair — drop both if
  // one is missing so we never restore half a selection.
  if (
    (out.selectedBucket && !out.selectedSessionId) ||
    (!out.selectedBucket && out.selectedSessionId)
  ) {
    delete out.selectedBucket;
    delete out.selectedSessionId;
  }
  return out;
}

/** Snapshot a `Persisted` blob from any state shape that includes the fields
 *  we care about. Caller passes the full store state; this picks the slice
 *  explicitly so adding a new store field never silently grows what we save. */
export function pickPersistable<T extends PersistableSource>(s: T): Persisted {
  return {
    selectedBucket: s.selectedBucket,
    selectedSessionId: s.selectedSessionId,
    scratchpadOpen: s.scratchpadOpen,
    expandedNodes: s.expandedNodes,
    promptWriterMode: s.promptWriter.mode,
  };
}

/** Debounced writer factory. The returned function takes a Persisted blob
 *  and schedules a single localStorage write within WRITE_DEBOUNCE_MS;
 *  duplicate-payload writes (same JSON) are skipped. */
export function makePersistenceWriter(): (next: Persisted) => void {
  let pending: number | null = null;
  let lastSerialized: string | null = null;
  let pendingPayload: Persisted | null = null;

  const flush = () => {
    pending = null;
    if (pendingPayload === null) return;
    const serialized = JSON.stringify(pendingPayload);
    pendingPayload = null;
    if (serialized === lastSerialized) return;
    lastSerialized = serialized;
    if (typeof localStorage === "undefined") return;
    try {
      localStorage.setItem(STORAGE_KEY, serialized);
    } catch {
      // Quota exceeded or storage disabled — silently skip; we'll try again
      // on the next state change.
    }
  };

  return (next) => {
    pendingPayload = next;
    if (pending !== null) return;
    pending = window.setTimeout(flush, WRITE_DEBOUNCE_MS);
  };
}

/** Source shape pickPersistable can read from. Matches the relevant slice
 *  of the app store. */
export interface PersistableSource {
  selectedBucket: string | null;
  selectedSessionId: string | null;
  scratchpadOpen: boolean;
  expandedNodes: Record<string, boolean>;
  promptWriter: { mode: PromptWriterMode };
}
