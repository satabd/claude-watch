import { create } from "zustand";
import type {
  AppSettings,
  ProjectMeta,
  PromptWriterContextMode,
  PromptWriterMode,
  ReviewEvidenceFlags,
  ReviewerMode,
  ScratchpadItem,
  SessionFull,
  TranscriptEvent,
} from "@/lib/api";
import { loadPersisted } from "@/lib/persisted-state";

interface AppState {
  projects: ProjectMeta[];
  setProjects: (p: ProjectMeta[]) => void;
  /** True after the first listProjects() call resolves (success or failure).
   *  Sidebar uses this to gate the "no projects" empty state so it doesn't
   *  flash before data arrives. */
  projectsLoaded: boolean;
  setProjectsLoaded: (b: boolean) => void;

  selectedBucket: string | null;
  selectedSessionId: string | null;
  selectSession: (bucket: string, sessionId: string) => void;
  /** Clear current selection. Used when a persisted/stale selection points at
   *  a session that no longer exists, or when the user explicitly deselects. */
  clearSelection: () => void;

  /** Sidebar tree: bucket -> open. Persisted across reloads. */
  expandedNodes: Record<string, boolean>;
  setNodeExpanded: (bucket: string, open: boolean) => void;

  session: SessionFull | null;
  setSession: (s: SessionFull | null) => void;
  /** True while api.getSession() is in flight for the current selection.
   *  Timeline uses this together with a session-id mismatch check to render
   *  a skeleton during transitions. */
  sessionLoading: boolean;
  setSessionLoading: (b: boolean) => void;
  appendEvent: (bucket: string, sessionId: string, ev: TranscriptEvent) => void;

  scratchpadOpen: boolean;
  toggleScratchpad: () => void;

  scratchpadItems: ScratchpadItem[];
  setScratchpadItems: (items: ScratchpadItem[]) => void;
  prependScratchpad: (item: ScratchpadItem) => void;
  removeScratchpadItem: (id: number) => void;

  // per-turn translation state — uuid -> { translation, model } or 'pending'
  translations: Record<string, { translation: string; model: string } | "pending">;
  setTranslation: (uuid: string, val: { translation: string; model: string } | "pending" | null) => void;
  mergeTranslations: (entries: Record<string, { translation: string; model: string }>) => void;
  // per-turn show-translated toggle
  shownTranslated: Record<string, boolean>;
  setShownTranslated: (uuid: string, on: boolean) => void;

  // SSE connection state
  sseConnected: boolean;
  setSseConnected: (b: boolean) => void;

  // theme
  theme: "light" | "dark";
  toggleTheme: () => void;

  // settings
  settings: AppSettings | null;
  setSettings: (s: AppSettings | null) => void;
  settingsOpen: boolean;
  setSettingsOpen: (b: boolean) => void;

  // session-level filters
  filterQuery: string; // substring search
  setFilterQuery: (q: string) => void;
  filterRoles: { user: boolean; assistant: boolean };
  setFilterRoles: (r: { user: boolean; assistant: boolean }) => void;
  filterTool: string | null; // null = all; otherwise tool name (Bash/Edit/...)
  setFilterTool: (t: string | null) => void;

  // session AI summary
  summary: { sessionId: string; text: string; model: string; cached: boolean } | null;
  setSummary: (s: AppState["summary"]) => void;
  summaryOpen: boolean;
  setSummaryOpen: (b: boolean) => void;

  // Prompt Writer
  promptWriter: {
    open: boolean;
    expanded: boolean;
    mode: PromptWriterMode;
    contextMode: PromptWriterContextMode;
    rough: string;
    sourceEventUuid: string | null;
    selectedText: string | null;
    /** Items the user has explicitly excluded from the auto-built pack (by id) */
    excludedItemIds: string[];
    currentDraftId: number | null;
  };
  openPromptWriter: (opts?: {
    sourceEventUuid?: string | null;
    selectedText?: string | null;
    rough?: string;
    mode?: PromptWriterMode;
    contextMode?: PromptWriterContextMode;
  }) => void;
  closePromptWriter: () => void;
  setPromptWriterField: <K extends keyof AppState["promptWriter"]>(
    key: K,
    value: AppState["promptWriter"][K]
  ) => void;
  togglePromptItemExcluded: (id: string) => void;

  // Review Threads panel (V1). The panel anchors to the Claude turn that
  // opened it (sourceTurn*), so the reviewer always sees the work the user
  // is asking about. We never auto-replay old review messages — short-term
  // continuity is owned by the provider session id stored on the thread.
  reviewPanel: {
    open: boolean;
    threadId: number | null;
    sourceTurnUuid: string | null;
    sourceTurnRole: string | null;
    sourceTurnText: string | null;
    reviewerMode: ReviewerMode;
    question: string;
    testOutput: string;
    buildOutput: string;
    evidence: ReviewEvidenceFlags;
  };
  openReviewPanel: (opts?: {
    sourceTurnUuid?: string | null;
    sourceTurnRole?: string | null;
    sourceTurnText?: string | null;
    threadId?: number | null;
  }) => void;
  closeReviewPanel: () => void;
  setReviewPanelField: <K extends keyof AppState["reviewPanel"]>(
    key: K,
    value: AppState["reviewPanel"][K]
  ) => void;
  setReviewEvidence: (key: keyof ReviewEvidenceFlags, value: boolean) => void;
}

// Pull the persisted UI slice once on module init. `loadPersisted()` is
// defensive — unknown / mistyped fields are dropped, so the worst case is
// `{}` and we fall back to defaults.
const _persisted = loadPersisted();

export const useApp = create<AppState>((set) => ({
  projects: [],
  setProjects: (projects) => set({ projects }),
  projectsLoaded: false,
  setProjectsLoaded: (projectsLoaded) => set({ projectsLoaded }),

  selectedBucket: _persisted.selectedBucket ?? null,
  selectedSessionId: _persisted.selectedSessionId ?? null,
  selectSession: (bucket, sessionId) =>
    set({ selectedBucket: bucket, selectedSessionId: sessionId }),
  clearSelection: () =>
    set({ selectedBucket: null, selectedSessionId: null, session: null, sessionLoading: false }),

  expandedNodes: _persisted.expandedNodes ?? {},
  setNodeExpanded: (bucket, open) =>
    set((s) => ({ expandedNodes: { ...s.expandedNodes, [bucket]: open } })),

  session: null,
  setSession: (session) => set({ session }),
  sessionLoading: false,
  setSessionLoading: (sessionLoading) => set({ sessionLoading }),
  appendEvent: (bucket, sessionId, ev) =>
    set((s) => {
      if (!s.session) return s;
      if (
        s.session.meta.bucket !== bucket ||
        s.session.meta.session_id !== sessionId
      ) {
        return s;
      }
      // Avoid duplicates by uuid
      if (s.session.events.some((e) => e.uuid === ev.uuid)) return s;
      return {
        session: {
          ...s.session,
          events: [...s.session.events, ev],
        },
      };
    }),

  scratchpadOpen: _persisted.scratchpadOpen ?? false,
  toggleScratchpad: () => set((s) => ({ scratchpadOpen: !s.scratchpadOpen })),

  scratchpadItems: [],
  setScratchpadItems: (scratchpadItems) => set({ scratchpadItems }),
  prependScratchpad: (item) =>
    set((s) => ({ scratchpadItems: [item, ...s.scratchpadItems] })),
  removeScratchpadItem: (id) =>
    set((s) => ({ scratchpadItems: s.scratchpadItems.filter((i) => i.id !== id) })),

  translations: {},
  setTranslation: (uuid, val) =>
    set((s) => {
      const t = { ...s.translations };
      if (val === null) delete t[uuid];
      else t[uuid] = val;
      return { translations: t };
    }),
  mergeTranslations: (entries) =>
    set((s) => ({ translations: { ...s.translations, ...entries } })),

  shownTranslated: {},
  setShownTranslated: (uuid, on) =>
    set((s) => ({ shownTranslated: { ...s.shownTranslated, [uuid]: on } })),

  sseConnected: false,
  setSseConnected: (sseConnected) => set({ sseConnected }),

  theme: (typeof localStorage !== "undefined" && (localStorage.getItem("theme") as any)) || "dark",
  toggleTheme: () =>
    set((s) => {
      const next = s.theme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem("theme", next);
      } catch {}
      return { theme: next };
    }),

  settings: null,
  setSettings: (settings) => set({ settings }),
  settingsOpen: false,
  setSettingsOpen: (settingsOpen) => set({ settingsOpen }),

  filterQuery: "",
  setFilterQuery: (filterQuery) => set({ filterQuery }),
  filterRoles: { user: true, assistant: true },
  setFilterRoles: (filterRoles) => set({ filterRoles }),
  filterTool: null,
  setFilterTool: (filterTool) => set({ filterTool }),

  summary: null,
  setSummary: (summary) => set({ summary }),
  summaryOpen: false,
  setSummaryOpen: (summaryOpen) => set({ summaryOpen }),

  promptWriter: {
    open: false,
    expanded: false,
    // Restore last-used writer mode. contextMode is intentionally NOT
    // restored — auto-resolution must run fresh each open so a stale
    // "full_session" choice can't surprise the user with a heavy / costly
    // context.
    mode: _persisted.promptWriterMode ?? "improve",
    contextMode: "auto",
    rough: "",
    sourceEventUuid: null,
    selectedText: null,
    excludedItemIds: [],
    currentDraftId: null,
  },
  openPromptWriter: (opts = {}) =>
    set((s) => ({
      promptWriter: {
        ...s.promptWriter,
        open: true,
        // Reset draft on each open if context changed
        currentDraftId: null,
        excludedItemIds: [],
        sourceEventUuid:
          opts.sourceEventUuid !== undefined
            ? opts.sourceEventUuid
            : s.promptWriter.sourceEventUuid,
        selectedText:
          opts.selectedText !== undefined
            ? opts.selectedText
            : s.promptWriter.selectedText,
        rough: opts.rough ?? s.promptWriter.rough,
        mode: opts.mode ?? s.promptWriter.mode,
        // Default to 'auto' on each open — auto-decision picks the right mode
        // based on entry point + rough input, saving tokens for simple rewrites.
        contextMode: opts.contextMode ?? "auto",
      },
    })),
  closePromptWriter: () =>
    set((s) => ({
      promptWriter: { ...s.promptWriter, open: false, expanded: false },
    })),
  setPromptWriterField: (key, value) =>
    set((s) => ({ promptWriter: { ...s.promptWriter, [key]: value } })),
  togglePromptItemExcluded: (id) =>
    set((s) => {
      const ex = new Set(s.promptWriter.excludedItemIds);
      if (ex.has(id)) ex.delete(id);
      else ex.add(id);
      return {
        promptWriter: { ...s.promptWriter, excludedItemIds: Array.from(ex) },
      };
    }),

  reviewPanel: {
    open: false,
    threadId: null,
    sourceTurnUuid: null,
    sourceTurnRole: null,
    sourceTurnText: null,
    reviewerMode: "critical",
    question: "",
    testOutput: "",
    buildOutput: "",
    evidence: {
      include_claude_turn: true,
      include_git_status: true,
      include_changed_files: true,
      include_git_diff: true,
      include_test_output: true,
      include_build_output: true,
    },
  },
  openReviewPanel: (opts = {}) =>
    set((s) => ({
      reviewPanel: {
        ...s.reviewPanel,
        open: true,
        threadId: opts.threadId !== undefined ? opts.threadId : s.reviewPanel.threadId,
        sourceTurnUuid:
          opts.sourceTurnUuid !== undefined
            ? opts.sourceTurnUuid
            : s.reviewPanel.sourceTurnUuid,
        sourceTurnRole:
          opts.sourceTurnRole !== undefined
            ? opts.sourceTurnRole
            : s.reviewPanel.sourceTurnRole,
        sourceTurnText:
          opts.sourceTurnText !== undefined
            ? opts.sourceTurnText
            : s.reviewPanel.sourceTurnText,
      },
    })),
  closeReviewPanel: () =>
    set((s) => ({
      reviewPanel: { ...s.reviewPanel, open: false },
    })),
  setReviewPanelField: (key, value) =>
    set((s) => ({ reviewPanel: { ...s.reviewPanel, [key]: value } })),
  setReviewEvidence: (key, value) =>
    set((s) => ({
      reviewPanel: {
        ...s.reviewPanel,
        evidence: { ...s.reviewPanel.evidence, [key]: value },
      },
    })),
}));
