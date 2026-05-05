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

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(base + url, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${txt}`);
  }
  return res.json() as Promise<T>;
}

export interface AppSettings {
  provider: string;
  available_providers: string[];
  default_models: Record<string, { fast: string | null; smart: string | null }>;
}

export const api = {
  listProjects: () =>
    jsonFetch<{ projects: ProjectMeta[] }>("/api/projects").then((d) => d.projects),
  getSettings: () => jsonFetch<AppSettings>("/api/settings"),
  updateSettings: (body: { provider?: string }) =>
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
    force?: boolean;
    provider?: string;
  }) =>
    jsonFetch<{
      summary: string;
      cached: boolean;
      model: string;
      content_hash: string;
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
};

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
