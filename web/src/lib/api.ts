// REST + types for the backend.

export interface SessionMeta {
  session_id: string;
  bucket: string;
  file_path: string;
  size_bytes: number;
  line_count: number | null;
  modified_ms: number;
  cwd: string | null;
  git_branch: string | null;
  entrypoint: string | null;
  version: string | null;
  ai_title: string | null;
  first_prompt: string | null;
  last_model: string | null;
  remote_name: string | null;
}

export interface ProjectMeta {
  bucket: string;
  display_name: string;
  cwd: string | null;
  last_modified_ms: number;
  session_count: number;
  sessions: SessionMeta[];
  /** Optional, currently never populated by the backend (only `SessionMeta`
   *  carries `git_branch`). Declared so the worktree-row UI can keep its
   *  forward-looking conditional render without a TS error; at runtime this
   *  is always `undefined`, so the branch label stays hidden until a future
   *  change starts populating it. */
  git_branch?: string | null;
}

export interface ToolUse {
  id: string;
  name: string;
  input: any;
}

export interface ToolResult {
  tool_use_id: string;
  content: any;
  is_error?: boolean;
}

export interface UsageInfo {
  input_tokens: number;
  output_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
}

export interface TranscriptEvent {
  type: string;
  uuid: string;
  parent_uuid: string | null;
  timestamp: string | null;
  session_id: string | null;
  cwd: string | null;
  git_branch: string | null;
  entrypoint: string | null;
  is_sidechain: boolean;
  role: "user" | "assistant" | null;
  text_blocks: string[];
  thinking_blocks: string[];
  tool_uses: ToolUse[];
  tool_results: ToolResult[];
  model: string | null;
  user_text: string | null;
  attribution_plugin: string | null;
  attribution_skill: string | null;
  pr: { number: number | null; url: string | null; repository: string | null } | null;
  ai_title: string | null;
  is_command_artifact: boolean;
  usage: UsageInfo | null;
}

export interface ScratchpadItem {
  id: number;
  action: string;
  source_text: string | null;
  source_turn: string | null;
  result: string;
  model: string | null;
  session_id: string | null;
  created_at: number;
}

export interface SessionFull {
  meta: SessionMeta;
  events: TranscriptEvent[];
}

const base = ""; // proxied through Vite

/** Error from a non-2xx API response. `detail` carries the parsed FastAPI
 *  detail payload (object or string) when the body was JSON, so callers can
 *  branch on structured flags instead of string-matching; `message` prefers
 *  the human-readable reason over the raw body. */
export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, statusText: string, body: string) {
    let detail: unknown = null;
    let msg = `${status} ${statusText}: ${body}`;
    try {
      const parsed = JSON.parse(body);
      detail = parsed?.detail ?? parsed;
      const d: any = detail;
      const reason =
        typeof d === "string" ? d : d?.reason ?? d?.message ?? null;
      if (typeof reason === "string" && reason) msg = reason;
    } catch {
      /* non-JSON body — keep raw message */
    }
    super(msg);
    this.status = status;
    this.detail = detail;
  }
}

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(base + url, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const txt = await res.text();
    throw new ApiError(res.status, res.statusText, txt);
  }
  return res.json() as Promise<T>;
}

export interface ModelChoice {
  id: string;
  label: string;
  note: string;
}

export interface TierInfo {
  key: string;
  label: string;
  note: string;
}

export interface AppSettings {
  provider: string;
  available_providers: string[];
  default_models: Record<string, { fast: string | null; smart: string | null }>;
  /** Models offerable per provider, ascending capability/cost. */
  available_models: Record<string, ModelChoice[]>;
  /** Tiers a model can be chosen for, and what each drives. */
  tiers: TierInfo[];
  /** What each provider/tier will actually use right now (override or default). */
  models: Record<string, Record<string, string | null>>;
}

export const api = {
  listProjects: () =>
    jsonFetch<{ projects: ProjectMeta[] }>("/api/projects").then((d) => d.projects),
  getSettings: () => jsonFetch<AppSettings>("/api/settings"),
  updateSettings: (body: {
    provider?: string;
    /** {"claude": {"fast": "sonnet"}} — null clears the override. */
    models?: Record<string, Record<string, string | null>>;
  }) =>
    jsonFetch<AppSettings>("/api/settings", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getSession: (bucket: string, sessionId: string) =>
    jsonFetch<SessionFull>(`/api/sessions/${bucket}/${sessionId}`),
  translate: (text: string, target_lang = "ar", force = false) =>
    jsonFetch<{
      translation: string;
      cached: boolean;
      model: string;
      target_lang: string;
      source_hash: string;
    }>("/api/translate", {
      method: "POST",
      body: JSON.stringify({ text, target_lang, force }),
    }),
  translateLookup: (text: string, target_lang = "ar") =>
    jsonFetch<{ hit: boolean; translation?: string; model?: string }>(
      "/api/translate/lookup",
      { method: "POST", body: JSON.stringify({ text, target_lang }) }
    ),
  translateLookupBatch: (
    items: { key: string; text: string }[],
    target_lang = "ar"
  ) =>
    jsonFetch<{ hits: Record<string, { translation: string; model: string }> }>(
      "/api/translate/lookup-batch",
      { method: "POST", body: JSON.stringify({ items, target_lang }) }
    ),
  scratchpadRun: (req: {
    action: "clarify" | "summarize" | "explain" | "glossary" | "comment";
    text: string;
    context_text?: string;
    note?: string;
    source_turn?: string;
    session_id?: string;
  }) =>
    jsonFetch<ScratchpadItem>("/api/scratchpad/run", {
      method: "POST",
      body: JSON.stringify(req),
    }),
  scratchpadList: (session_id?: string) => {
    const q = session_id ? `?session_id=${encodeURIComponent(session_id)}` : "";
    return jsonFetch<{ items: ScratchpadItem[] }>(`/api/scratchpad${q}`).then((d) => d.items);
  },
  scratchpadDelete: (id: number) =>
    jsonFetch<{ ok: boolean }>(`/api/scratchpad/${id}`, { method: "DELETE" }),
  summarizeSession: (req: {
    session_id: string;
    transcript: string;
    /** Lets the server ask the session itself instead of re-sending the
     *  transcript; `transcript` stays the cache key and the fallback. */
    bucket?: string;
    in_session?: boolean;
    force?: boolean;
    provider?: string;
  }) =>
    jsonFetch<{
      summary: string;
      cached: boolean;
      model: string;
      content_hash: string;
      /** Where the answer came from: the live pane, a headless resume of the
       *  session, the pasted transcript, or the cache. */
      source: "pane" | "resume" | "transcript" | "cache";
    }>("/api/summarize-session", { method: "POST", body: JSON.stringify(req) }),

  // ---- Prompt Writer ----
  promptWriterGenerate: (req: PromptWriterGenerateRequest) =>
    jsonFetch<PromptDraft>("/api/prompt-writer/generate", {
      method: "POST",
      body: JSON.stringify(req),
    }),
  promptWriterRefine: (req: { draft_id: number; refinement: string; provider?: string }) =>
    jsonFetch<PromptDraft>("/api/prompt-writer/refine", {
      method: "POST",
      body: JSON.stringify(req),
    }),
  promptWriterPatch: (id: number, body: { generated_prompt?: string; improvement_notes?: string }) =>
    jsonFetch<PromptDraft>(`/api/prompt-writer/drafts/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  promptWriterList: (session_id?: string, limit = 30) => {
    const q = new URLSearchParams();
    if (session_id) q.set("session_id", session_id);
    q.set("limit", String(limit));
    return jsonFetch<{ items: PromptDraft[] }>(`/api/prompt-writer/drafts?${q}`).then(
      (d) => d.items
    );
  },
  promptWriterGet: (id: number) =>
    jsonFetch<PromptDraft>(`/api/prompt-writer/drafts/${id}`),
  promptWriterDelete: (id: number) =>
    jsonFetch<{ ok: boolean }>(`/api/prompt-writer/drafts/${id}`, { method: "DELETE" }),

  // ---- Remote hosts ----
  remotesList: () =>
    jsonFetch<{ items: RemoteHost[] }>("/api/remotes").then((d) => d.items),
  remotesCreate: (req: RemoteHostCreate) =>
    jsonFetch<RemoteHost>("/api/remotes", {
      method: "POST",
      body: JSON.stringify(req),
    }),
  remotesUpdate: (id: number, req: Partial<RemoteHostCreate> & { enabled?: boolean }) =>
    jsonFetch<RemoteHost>(`/api/remotes/${id}`, {
      method: "PATCH",
      body: JSON.stringify(req),
    }),
  remotesDelete: (id: number) =>
    jsonFetch<{ ok: boolean; files_removed: number }>(`/api/remotes/${id}`, {
      method: "DELETE",
    }),
  remotesTest: (id: number) =>
    jsonFetch<RemoteTestResult>(`/api/remotes/${id}/test`, { method: "POST" }),
  remotesSync: (id: number) =>
    jsonFetch<RemoteSyncReport>(`/api/remotes/${id}/sync`, { method: "POST" }),
  remotesDiscoverWsl: () =>
    jsonFetch<{ items: WslDistroInfo[]; suggestions: RemoteHostCreate[] }>(
      "/api/remotes/discover-wsl",
      { method: "POST" }
    ),

  // ---- Review Threads ----
  reviewsListSkills: () =>
    jsonFetch<ReviewSkillsList>("/api/reviews/skills"),
  reviewsList: (project_bucket?: string) => {
    const q = new URLSearchParams();
    if (project_bucket) q.set("project_bucket", project_bucket);
    const path = `/api/reviews/threads${q.toString() ? "?" + q : ""}`;
    return jsonFetch<ReviewThread[]>(path);
  },
  reviewsCreateThread: (req: ReviewThreadCreate) =>
    jsonFetch<ReviewThread>("/api/reviews/threads", {
      method: "POST",
      body: JSON.stringify(req),
    }),
  reviewsPatchThread: (id: number, req: { name?: string; archived?: boolean }) =>
    jsonFetch<ReviewThread>(`/api/reviews/threads/${id}`, {
      method: "PATCH",
      body: JSON.stringify(req),
    }),
  reviewsListMessages: (threadId: number) =>
    jsonFetch<ReviewMessage[]>(`/api/reviews/threads/${threadId}/messages`),
  reviewsPreview: (req: ReviewPreviewRequest) =>
    jsonFetch<ReviewPreview>("/api/reviews/preview", {
      method: "POST",
      body: JSON.stringify(req),
    }),
  reviewsSend: (req: ReviewSendRequest) =>
    jsonFetch<ReviewMessage>("/api/reviews/send", {
      method: "POST",
      body: JSON.stringify(req),
    }),

  // --- Zellij runtime control -------------------------------------------
  runtimeState: (bucket: string, sessionId: string) =>
    jsonFetch<RuntimeState>(`/api/runtime/${bucket}/${sessionId}/state`),
  /** Start a brand-new Claude session in a project, managed from birth.
   *  claude-watch picks the session id, so it is owned immediately rather
   *  than discovered later as an anonymous external process.
   *
   *  `prompt` is the session's first turn. Claude writes no transcript until
   *  a session has one, and the sidebar is built from transcripts — so
   *  without a prompt the session is real and managed but not yet
   *  selectable. */
  newSession: (bucket: string, prompt?: string, title?: string) =>
    jsonFetch<{
      session_id: string;
      bucket: string;
      cwd: string;
      /** False while Claude has not written its transcript yet — usually the
       *  first-run trust prompt. The session is unselectable until it does. */
      transcript_ready: boolean;
      blocked_on: { question: string; options: { n: string; label: string }[] } | null;
      attach_command: string | null;
      state: RuntimeState;
    }>(`/api/runtime/${bucket}/sessions`, {
      method: "POST",
      body: JSON.stringify({ title: title ?? null, prompt: prompt ?? null }),
    }),

  /** Resume a session into a managed pane. Refuses (409) if any claude is
   *  still alive on the transcript — there is no takeover. */
  runtimeControl: (bucket: string, sessionId: string) =>
    jsonFetch<RuntimeState>(`/api/runtime/${bucket}/${sessionId}/control`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  pendingList: (bucket: string, sessionId: string) =>
    jsonFetch<{ pending: PendingPrompt[] }>(
      `/api/runtime/${bucket}/${sessionId}/pending`
    ).then((d) => d.pending),
  pendingCreate: (bucket: string, sessionId: string, text: string) =>
    jsonFetch<PendingPrompt>(`/api/runtime/${bucket}/${sessionId}/pending`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  pendingEdit: (id: number, text: string) =>
    jsonFetch<PendingPrompt>(`/api/runtime/pending/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ text }),
    }),
  pendingDelete: (id: number) =>
    jsonFetch<{ ok: boolean }>(`/api/runtime/pending/${id}`, {
      method: "DELETE",
    }),
  pendingSend: (bucket: string, sessionId: string, id: number) =>
    jsonFetch<{ ok: boolean; id: number }>(
      `/api/runtime/${bucket}/${sessionId}/pending/${id}/send`,
      { method: "POST" }
    ),
  runtimeInterrupt: (bucket: string, sessionId: string) =>
    jsonFetch<{ ok: boolean }>(`/api/runtime/${bucket}/${sessionId}/interrupt`, {
      method: "POST",
    }),
  runtimeRespond: (bucket: string, sessionId: string, choice: string) =>
    jsonFetch<{ ok: boolean }>(`/api/runtime/${bucket}/${sessionId}/respond`, {
      method: "POST",
      body: JSON.stringify({ choice }),
    }),
  runtimeRelease: (bucket: string, sessionId: string) =>
    jsonFetch<{
      released: boolean;
      reason?: string;
      zellij_session?: string;
      pane_id?: string;
      /** Processes that had to be signalled because closing the pane left
       *  them running — an orphan keeps writing to the transcript. */
      reaped_pids?: number[];
      surviving_pids?: number[];
    }>(`/api/runtime/${bucket}/${sessionId}/release`, { method: "POST" }),
  runtimeSetMode: (bucket: string, sessionId: string, mode: PermissionMode) =>
    jsonFetch<{ ok: boolean; mode: PermissionMode }>(
      `/api/runtime/${bucket}/${sessionId}/mode`,
      { method: "POST", body: JSON.stringify({ mode }) }
    ),
};

/** Claude's permission modes. Everything except `bypass` is reachable from
 *  the TUI's Shift+Tab cycle — which modes the cycle actually visits varies
 *  by claude build, so the server presses-and-verifies rather than assuming,
 *  and reports what the loop contained if a mode turns out to be missing. */
export type PermissionMode =
  | "manual"
  | "accept_edits"
  | "plan"
  | "auto"
  | "dont_ask"
  | "bypass";

export interface RuntimeState {
  state: "managed" | "external_idle" | "external_busy" | "inactive" | "resumable";
  controllable: boolean;
  reason: string | null;
  zellij_session: string | null;
  pane_id: string | null;
  /** `<project>-<session>` as shown on the zellij tab and pane. */
  pane_title: string | null;
  /** Name of the zellij tab holding the pane. Recorded when the tab is
   *  created — zellij can list tab names and pane ids but never maps one to
   *  the other, so this is null for panes adopted from an older layout. */
  zellij_tab: string | null;
  /** Paste-in-a-terminal command to watch this very pane. */
  attach_command: string | null;
  external_pid: number | null;
  busy: boolean;
  /** Present when the managed claude TUI is blocked on an interactive
   *  dialog (permission prompt / question with numbered options). */
  awaiting_input: {
    question: string;
    options: { n: string; label: string }[];
  } | null;
  /** Permission mode read from the managed TUI's status line. */
  mode: PermissionMode | null;
  mode_label: string | null;
  /** True while a turn is in flight — read live from the pane, so it
   *  reacts immediately rather than lagging behind JSONL writes. */
  working: boolean;
  /** Live spinner text from the TUI while working. */
  activity: { verb: string; elapsed_s: number; detail: string | null } | null;
}

export interface PendingPrompt {
  id: number;
  bucket: string;
  session_id: string;
  text: string;
  status: string;
  created_ms: number;
  updated_ms: number;
  sent_ms: number | null;
}

export interface WslDistroInfo {
  name: string;
  state: string;
  is_default: boolean;
  user: string | null;
  home_dir: string | null;
  ssh_running: boolean;
  ssh_port: number;
  sshd_installed: boolean;
  suggested_projects_path: string | null;
  error: string | null;
  hint: string | null;
}

// ---- Remote host types ----

export interface RemoteHost {
  id: number;
  name: string;
  host: string;
  port: number;
  username: string;
  key_path: string | null;
  projects_path: string | null;
  home_dir: string | null;
  platform: string | null;
  enabled: number; // sqlite stores as 0/1
  last_synced_ms: number | null;
  last_error: string | null;
  created_at: number;
  updated_at: number;
  // Reliability fields written by the remote watcher
  status: string | null;          // "live", "connecting", "reconnecting in 8s", "disabled", "stopped", ...
  last_poll_ms: number | null;    // most recent successful poll cycle
  last_event_ms: number | null;   // most recent observed change on the remote
  next_retry_ms: number | null;   // when in backoff, when the next retry will fire
}

export interface RemoteHostCreate {
  name: string;
  host: string;
  port: number;
  username: string;
  key_path?: string | null;
  projects_path?: string | null;
}

export interface RemoteTestResult {
  ok: boolean;
  error?: string;
  home_dir?: string | null;
  platform?: string | null;
  projects_path?: string;
  projects_exists?: boolean;
  bucket_count?: number;
}

export interface RemoteSyncReport {
  host_name: string;
  ok: boolean;
  error: string | null;
  home_dir: string | null;
  platform: string | null;
  discovered_buckets: number;
  files_seen: number;
  files_downloaded: number;
  files_unchanged: number;
  bytes_downloaded: number;
  elapsed_ms: number;
  detail: string[];
}

// ---- Prompt Writer types ----

export type PromptWriterMode =
  | "improve"
  | "clarify"
  | "developer_task"
  | "bug_report"
  | "design"
  | "critical_review"
  | "short_command"
  | "continue";

export type PromptWriterContextMode =
  // Adaptive set (current)
  | "auto"
  | "none"
  | "current_item"
  | "selected"
  | "recent_small"
  | "focused"
  | "expanded"
  | "full_session"
  // Legacy aliases (still accepted by backend; UI normalizes them)
  | "recent"
  | "selected_plus_nearby"
  | "summary"
  | "full_feature";

export interface ContextPackItem {
  id: string;
  kind: "project" | "session" | "selected" | "message" | "summary" | "note";
  label: string;
  text: string;
}

export interface PromptWriterGenerateRequest {
  bucket: string;
  session_id: string;
  source_event_uuid?: string;
  mode: PromptWriterMode;
  context_mode: PromptWriterContextMode;
  rough_input: string;
  context_pack: ContextPackItem[];
  provider?: string;
}

export interface PromptDraft {
  id: number;
  bucket: string;
  session_id: string;
  source_event_uuid: string | null;
  mode: PromptWriterMode;
  context_mode: PromptWriterContextMode;
  rough_input: string;
  generated_prompt: string;
  improvement_notes: string | null;
  context_used: string | null;
  context_chars: number | null;
  model: string | null;
  created_at: number;
  updated_at: number;
}

// ===== Review Threads =====

/** Legacy alias — older callers said "Reviewer mode". The new vocabulary
 *  is "Review Skill"; the typed values map 1:1 onto the backend SkillId. */
export type ReviewerMode = SkillId;

export type SkillId =
  | "next_prompt_coach"
  | "quick_review"
  | "critical_review"
  | "prompt_coach";

export interface ReviewSkill {
  id: SkillId;
  label: string;
  purpose: string;
}

export interface ReviewSkillsList {
  default_skill_id: SkillId;
  skill_version: number;
  skills: ReviewSkill[];
}

export interface ReviewThread {
  id: number;
  name: string;
  provider: string;
  project_bucket: string | null;
  claude_session_id: string | null;
  provider_session_id: string | null;
  /** The skill the thread was last sent under (or null for legacy
   *  threads that predate the skill system). */
  active_skill_id: SkillId | null;
  /** The (skill_id, version) pair the stored Codex provider session was
   *  created with. The send route refuses to resume a session whose
   *  pair doesn't match the current request. */
  provider_session_skill_id: SkillId | null;
  provider_session_skill_version: number | null;
  created_at: number;
  updated_at: number;
  archived_at: number | null;
}

export interface ReviewThreadCreate {
  name: string;
  project_bucket?: string | null;
  claude_session_id?: string | null;
  provider?: string;
}

export interface ReviewMessage {
  id: number;
  thread_id: number;
  role: "user" | "reviewer" | string;
  content: string;
  source_session_id: string | null;
  source_turn_uuid: string | null;
  context_used_json: Record<string, unknown> | null;
  evidence_used_json: Record<string, unknown> | null;
  provider: string | null;
  model: string | null;
  estimated_tokens: number | null;
  provider_tokens: number | null;
  created_at: number;
}

export interface ReviewEvidenceFlags {
  include_claude_turn: boolean;
  include_git_status: boolean;
  include_changed_files: boolean;
  include_git_diff: boolean;
  include_test_output: boolean;
  include_build_output: boolean;
}

export interface ReviewPreviewRequest {
  question: string;
  /** Preferred. Backend will fall back to ``reviewer_mode`` for legacy
   *  callers; new code should always send ``skill_id``. */
  skill_id: SkillId;
  /** Legacy field; kept for back-compat. New callers should leave it
   *  unset and use ``skill_id`` instead. */
  reviewer_mode?: ReviewerMode;
  project_bucket?: string | null;
  project_cwd?: string | null;
  claude_session_id?: string | null;
  claude_turn_uuid?: string | null;
  claude_turn_role?: string | null;
  claude_turn_text?: string | null;
  test_output?: string | null;
  build_output?: string | null;
  evidence: ReviewEvidenceFlags;
}

export interface ReviewSecretHit {
  label: string;
  location: string;
}

export interface ReviewGitSummary {
  is_repo: boolean;
  branch: string | null;
  ahead: number;
  behind: number;
  dirty_count: number;
  diff_byte_count: number;
  diff_truncated: boolean;
}

export interface ReviewPreview {
  byte_count: number;
  estimated_tokens: number;
  git: ReviewGitSummary;
  secret_hits: ReviewSecretHit[];
  prompt_preview: string;
  skill_id: SkillId;
  skill_version: number;
}

export interface ReviewSendRequest extends ReviewPreviewRequest {
  thread_id: number;
  secret_override?: boolean;
}
