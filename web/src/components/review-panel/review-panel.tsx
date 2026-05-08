import * as React from "react";
import {
  AlertCircle,
  ClipboardCheck,
  Copy,
  Eye,
  Loader2,
  MessageSquare,
  Plus,
  Send,
  ShieldAlert,
} from "lucide-react";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useApp } from "@/store/app";
import {
  api,
  type ReviewEvidenceFlags,
  type ReviewMessage,
  type ReviewPreview,
  type ReviewSecretHit,
  type ReviewThread,
  type ReviewerMode,
} from "@/lib/api";
import { cn, formatRelative } from "@/lib/utils";
import { sessionDisplayName } from "@/lib/session-display";
import { copyTargetForReply } from "@/lib/extract-next-prompt";
import { effectiveQuestion } from "./effective-question";
import { toast } from "sonner";

const REVIEWER_MODES: { id: ReviewerMode; label: string; hint: string }[] = [
  {
    id: "critical",
    label: "Critical Reviewer",
    hint: "Risks, missing tests, scope creep, next prompt",
  },
  {
    id: "prompt_coach",
    label: "Prompt Coach",
    hint: "Clarify intent and write a stronger prompt",
  },
];

const EVIDENCE_LABELS: { key: keyof ReviewEvidenceFlags; label: string }[] = [
  { key: "include_claude_turn", label: "Claude result" },
  { key: "include_git_status", label: "Git status" },
  { key: "include_changed_files", label: "Changed files" },
  { key: "include_git_diff", label: "Git diff" },
  { key: "include_test_output", label: "Test output" },
  { key: "include_build_output", label: "Build output" },
];

const PREVIEW_DEBOUNCE_MS = 350;


/** Build the default thread name for an auto-create. The spec wants two
 *  shapes: "Review: <first 60 chars of result>" when anchored to a turn,
 *  "Review: <session display>" when opened from the toolbar. We use the
 *  session display as a fallback when the turn text is empty. */
function defaultThreadName(args: {
  fromTurn: boolean;
  turnText: string | null;
  sessionMeta: ReturnType<typeof useApp.getState>["session"] extends infer S
    ? S extends { meta: infer M }
      ? M
      : null
    : null;
}): string {
  const sessionLabel = args.sessionMeta
    ? sessionDisplayName(args.sessionMeta as any, 60)
    : "session";
  if (args.fromTurn && args.turnText && args.turnText.trim()) {
    const snippet = args.turnText.trim().replace(/\s+/g, " ").slice(0, 60);
    return `Review: ${snippet}${args.turnText.length > 60 ? "…" : ""}`;
  }
  return `Review: ${sessionLabel}`;
}

type SetupState = "idle" | "loading" | "ready" | "error";

export function ReviewPanel() {
  const session = useApp((s) => s.session);
  const open = useApp((s) => s.reviewPanel.open);
  const close = useApp((s) => s.closeReviewPanel);
  const panel = useApp((s) => s.reviewPanel);
  const setField = useApp((s) => s.setReviewPanelField);
  const setEvidence = useApp((s) => s.setReviewEvidence);

  const sessionMeta = session?.meta ?? null;
  const projectBucket = sessionMeta?.bucket ?? null;
  const claudeSessionId = sessionMeta?.session_id ?? null;
  const projectCwd = sessionMeta?.cwd ?? null;

  const [threads, setThreads] = React.useState<ReviewThread[]>([]);
  const [messages, setMessages] = React.useState<ReviewMessage[]>([]);
  const [preview, setPreview] = React.useState<ReviewPreview | null>(null);
  const [previewing, setPreviewing] = React.useState(false);
  const [sending, setSending] = React.useState(false);
  const [creatingThread, setCreatingThread] = React.useState(false);
  const [secretOverride, setSecretOverride] = React.useState(false);
  const [setupState, setSetupState] = React.useState<SetupState>("idle");
  const [setupError, setSetupError] = React.useState<string | null>(null);

  const activeThread = threads.find((t) => t.id === panel.threadId) ?? null;

  /** Find the most recent active thread for (bucket, claude_session_id) and
   *  select it; otherwise auto-create one. Runs once per panel open. Calls
   *  ONLY DB endpoints — never the reviewer LLM, so opening the panel never
   *  spends Codex tokens. */
  const ensureThread = React.useCallback(
    async (
      sourceTurnUuid: string | null,
      sourceTurnText: string | null,
    ): Promise<void> => {
      setSetupState("loading");
      setSetupError(null);
      try {
        const list = await api.reviewsList(projectBucket ?? undefined);
        setThreads(list);
        // Match by bucket + claude_session_id + not archived. Backend already
        // filters by bucket; we add the session filter on the client so that
        // distinct sessions in the same project never accidentally share a
        // default thread.
        const match = list.find(
          (t) =>
            !t.archived_at &&
            (claudeSessionId == null ||
              t.claude_session_id === claudeSessionId),
        );
        if (match) {
          setField("threadId", match.id);
          setSetupState("ready");
          return;
        }
        // No match — auto-create. This is a DB-only operation; the reviewer
        // LLM is only invoked on /send.
        const name = defaultThreadName({
          fromTurn: !!sourceTurnUuid,
          turnText: sourceTurnText,
          sessionMeta,
        });
        const created = await api.reviewsCreateThread({
          name,
          project_bucket: projectBucket,
          claude_session_id: claudeSessionId,
        });
        setThreads((prev) => [created, ...prev]);
        setField("threadId", created.id);
        setSetupState("ready");
      } catch (e: any) {
        setSetupError(e?.message ?? "Could not prepare a review thread");
        setSetupState("error");
      }
    },
    [projectBucket, claudeSessionId, sessionMeta, setField],
  );

  // Trigger setup once each time the panel opens. Reset on close so the
  // next open re-validates against the (possibly different) current
  // session. We use a ref so React's StrictMode double-invoke doesn't
  // create two threads.
  const setupAttemptedRef = React.useRef(false);
  React.useEffect(() => {
    if (!open) {
      setupAttemptedRef.current = false;
      setSetupState("idle");
      setSetupError(null);
      return;
    }
    if (!setupAttemptedRef.current) {
      setupAttemptedRef.current = true;
      ensureThread(panel.sourceTurnUuid, panel.sourceTurnText);
    }
  }, [open, ensureThread, panel.sourceTurnUuid, panel.sourceTurnText]);

  // Load messages when active thread changes.
  React.useEffect(() => {
    if (!panel.threadId) {
      setMessages([]);
      return;
    }
    api
      .reviewsListMessages(panel.threadId)
      .then(setMessages)
      .catch((e) => toast.error(e?.message ?? "Failed to load messages"));
  }, [panel.threadId]);

  // Build the preview request from current store state. We send the
  // *effective* question (default-when-blank) so the size / token estimate
  // already reflects what /send would actually transmit. No API call ever
  // sees question="" from this component.
  const previewRequest = React.useMemo(
    () => ({
      question: effectiveQuestion(panel.question),
      reviewer_mode: panel.reviewerMode,
      project_bucket: projectBucket,
      project_cwd: projectCwd,
      claude_session_id: claudeSessionId,
      claude_turn_uuid: panel.sourceTurnUuid,
      claude_turn_role: panel.sourceTurnRole,
      claude_turn_text: panel.sourceTurnText,
      test_output: panel.testOutput || null,
      build_output: panel.buildOutput || null,
      evidence: panel.evidence,
    }),
    [
      panel.question,
      panel.reviewerMode,
      panel.sourceTurnUuid,
      panel.sourceTurnRole,
      panel.sourceTurnText,
      panel.testOutput,
      panel.buildOutput,
      panel.evidence,
      projectBucket,
      projectCwd,
      claudeSessionId,
    ],
  );

  // Debounced preview refresh — fires whenever evidence / question changes.
  // Always issues the request even when the user hasn't typed yet, because
  // previewRequest already substitutes DEFAULT_QUESTION; the size estimate
  // therefore reflects what would actually be sent on click.
  React.useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setPreviewing(true);
    const handle = window.setTimeout(async () => {
      try {
        const p = await api.reviewsPreview(previewRequest);
        if (!cancelled) setPreview(p);
      } catch (e: any) {
        if (!cancelled) toast.error(e?.message ?? "Preview failed");
      } finally {
        if (!cancelled) setPreviewing(false);
      }
    }, PREVIEW_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [open, previewRequest]);

  /** "New thread" button — explicit user action to start a SEPARATE thread
   *  alongside whatever auto-setup picked. We disambiguate the name with a
   *  short timestamp so two manually-created threads in the same minute
   *  don't end up with identical labels. */
  const onCreateThread = async () => {
    setCreatingThread(true);
    try {
      const base = defaultThreadName({
        fromTurn: !!panel.sourceTurnUuid,
        turnText: panel.sourceTurnText,
        sessionMeta,
      });
      const stamp = new Date().toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
      });
      const t = await api.reviewsCreateThread({
        name: `${base} (${stamp})`,
        project_bucket: projectBucket,
        claude_session_id: claudeSessionId,
      });
      setThreads((prev) => [t, ...prev]);
      setField("threadId", t.id);
      setMessages([]);
      toast.success(`Created thread #${t.id}`);
    } catch (e: any) {
      toast.error(e?.message ?? "Create thread failed");
    } finally {
      setCreatingThread(false);
    }
  };

  const onSend = async () => {
    if (!panel.threadId) {
      toast.error("Create or select a thread first");
      return;
    }
    if (preview?.secret_hits?.length && !secretOverride) {
      toast.error(
        "Secret-like values detected. Toggle override or remove the offending evidence.",
      );
      return;
    }
    // Empty / whitespace-only questions are silently substituted with
    // DEFAULT_QUESTION via effectiveQuestion() inside previewRequest.
    // The frontend never calls /send with question="" so the backend's
    // pydantic min_length=1 validator never trips for that reason.
    setSending(true);
    try {
      const reply = await api.reviewsSend({
        thread_id: panel.threadId,
        secret_override: secretOverride,
        ...previewRequest,
      });
      setMessages((prev) => [...prev, reply]);
      setField("question", "");
      setSecretOverride(false);
      // Refresh thread list so the "resume" badge reflects the freshly-stored
      // provider_session_id. Pure DB read, no Codex call.
      api
        .reviewsList(projectBucket ?? undefined)
        .then(setThreads)
        .catch(() => {});
      toast.success("Reviewer replied");
    } catch (e: any) {
      // 409 SECRET_DETECTED carries hits — surface them and let the user
      // either edit or override.
      const msg = String(e?.message ?? "");
      if (msg.includes("SECRET_DETECTED") || msg.startsWith("409")) {
        toast.error(
          "Secret-like values detected. Toggle override or remove the offending evidence.",
        );
      } else if (msg.startsWith("422")) {
        // Backend pydantic validation rejected something. Replace the raw
        // JSON-shaped error with a friendly hint. The default-question
        // substitution should keep us out of this branch in practice.
        toast.error(
          "The reviewer couldn't validate the request. Try rephrasing your question or simplifying the evidence and retry.",
        );
      } else {
        toast.error(msg || "Send failed");
      }
    } finally {
      setSending(false);
    }
  };

  const onCopyNextPrompt = async (reply: string) => {
    try {
      await navigator.clipboard.writeText(copyTargetForReply(reply));
      toast.success("Copied next prompt");
    } catch {
      toast.error("Clipboard write failed");
    }
  };

  return (
    <Sheet open={open} onOpenChange={(o) => (o ? null : close())}>
      <SheetContent
        side="right"
        className="flex h-full flex-col p-0 sm:max-w-[820px]"
      >
        <header className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-3">
          <ClipboardCheck className="h-4 w-4 text-primary" />
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold">Review Threads</div>
            <div className="truncate text-[11px] text-muted-foreground">
              {projectCwd ?? "No project selected"}
              {panel.sourceTurnUuid && (
                <span className="ms-2 rounded bg-muted px-1 font-mono">
                  anchored to turn {panel.sourceTurnUuid.slice(0, 8)}
                </span>
              )}
            </div>
          </div>
          <Badge variant="muted">codex</Badge>
        </header>

        <div className="flex-1 min-h-0 overflow-hidden grid grid-cols-[220px_1fr]">
          <ThreadList
            threads={threads}
            activeId={panel.threadId}
            onSelect={(id) => setField("threadId", id)}
            onCreate={onCreateThread}
            creating={creatingThread}
            setupState={setupState}
            setupError={setupError}
            onRetrySetup={() =>
              ensureThread(panel.sourceTurnUuid, panel.sourceTurnText)
            }
          />

          <div className="flex min-w-0 flex-col overflow-y-auto scrollbar-thin">
            <ReviewerModeRow
              mode={panel.reviewerMode}
              onChange={(m) => setField("reviewerMode", m)}
            />

            <EvidencePanel
              evidence={panel.evidence}
              onToggle={setEvidence}
              hasClaudeTurn={!!panel.sourceTurnText}
              git={preview?.git}
              testOutput={panel.testOutput}
              buildOutput={panel.buildOutput}
              onChangeTest={(v) => setField("testOutput", v)}
              onChangeBuild={(v) => setField("buildOutput", v)}
            />

            <PacketSizeRow preview={preview} previewing={previewing} />

            {preview?.secret_hits?.length ? (
              <SecretWarning
                hits={preview.secret_hits}
                override={secretOverride}
                onToggleOverride={() => setSecretOverride((v) => !v)}
              />
            ) : null}

            <QuestionRow
              question={panel.question}
              onChange={(v) => setField("question", v)}
              onSend={onSend}
              sending={sending}
              disabled={!activeThread}
            />

            <MessagesList
              messages={messages}
              onCopyNextPrompt={onCopyNextPrompt}
            />
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

// -- subcomponents --

function ThreadList({
  threads,
  activeId,
  onSelect,
  onCreate,
  creating,
  setupState,
  setupError,
  onRetrySetup,
}: {
  threads: ReviewThread[];
  activeId: number | null;
  onSelect: (id: number) => void;
  onCreate: () => void;
  creating: boolean;
  setupState: SetupState;
  setupError: string | null;
  onRetrySetup: () => void;
}) {
  return (
    <aside className="flex flex-col border-e border-border bg-card/40">
      <div className="flex items-center justify-between gap-2 px-3 py-2 text-[11px] uppercase tracking-wider text-muted-foreground">
        <span>Threads</span>
        <Button
          size="sm"
          variant="outline"
          className="h-6"
          onClick={onCreate}
          disabled={creating || setupState === "loading"}
          title="Start an additional separate review thread"
        >
          {creating ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Plus className="h-3 w-3" />
          )}
          New
        </Button>
      </div>
      <div className="flex-1 overflow-y-auto scrollbar-thin">
        {setupState === "loading" && threads.length === 0 && (
          <div className="flex items-center gap-2 px-3 py-2 text-[11px] text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" />
            Preparing review thread…
          </div>
        )}
        {setupState === "error" && (
          <div className="m-2 rounded-md border border-destructive/40 bg-destructive/10 p-2 text-[11px] text-destructive">
            <div className="mb-1 flex items-center gap-1.5 font-medium">
              <AlertCircle className="h-3 w-3" />
              Couldn't prepare a review thread
            </div>
            <div className="mb-2 break-words text-[10.5px] opacity-90">
              {setupError ?? "Unknown error"}
            </div>
            <Button
              size="sm"
              variant="outline"
              className="h-6"
              onClick={onRetrySetup}
            >
              Retry
            </Button>
          </div>
        )}
        {threads.map((t) => (
          <button
            key={t.id}
            onClick={() => onSelect(t.id)}
            className={cn(
              "flex w-full flex-col gap-0.5 rounded px-3 py-2 text-left text-[12px] transition-colors hover:bg-accent/50",
              activeId === t.id && "bg-accent text-foreground",
            )}
            title={t.name}
          >
            <span className="truncate font-medium">{t.name}</span>
            <span className="text-[10px] text-muted-foreground">
              {formatRelative(t.updated_at)}
              {t.provider_session_id && (
                <span className="ms-1 font-mono opacity-60">· resume</span>
              )}
            </span>
          </button>
        ))}
      </div>
    </aside>
  );
}

function ReviewerModeRow({
  mode,
  onChange,
}: {
  mode: ReviewerMode;
  onChange: (m: ReviewerMode) => void;
}) {
  return (
    <div className="border-b border-border px-4 py-3">
      <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        Reviewer mode
      </div>
      <div className="flex flex-wrap gap-2">
        {REVIEWER_MODES.map((m) => (
          <button
            key={m.id}
            onClick={() => onChange(m.id)}
            className={cn(
              "rounded-md border px-2.5 py-1 text-left text-[12px]",
              mode === m.id
                ? "border-primary bg-primary/10 text-foreground"
                : "border-border text-muted-foreground hover:bg-accent/40",
            )}
            title={m.hint}
          >
            <div className="font-medium">{m.label}</div>
            <div className="text-[10.5px] text-muted-foreground">{m.hint}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

function EvidencePanel({
  evidence,
  onToggle,
  hasClaudeTurn,
  git,
  testOutput,
  buildOutput,
  onChangeTest,
  onChangeBuild,
}: {
  evidence: ReviewEvidenceFlags;
  onToggle: (key: keyof ReviewEvidenceFlags, value: boolean) => void;
  hasClaudeTurn: boolean;
  git: ReviewPreview["git"] | undefined;
  testOutput: string;
  buildOutput: string;
  onChangeTest: (v: string) => void;
  onChangeBuild: (v: string) => void;
}) {
  return (
    <div className="border-b border-border px-4 py-3">
      <div className="mb-1.5 flex items-center gap-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        <Eye className="h-3 w-3" /> Evidence
      </div>
      <div className="grid grid-cols-2 gap-1.5">
        {EVIDENCE_LABELS.map((e) => {
          const disabled =
            (e.key === "include_claude_turn" && !hasClaudeTurn) ||
            (e.key === "include_git_status" && !git?.is_repo) ||
            (e.key === "include_git_diff" && !git?.is_repo) ||
            (e.key === "include_changed_files" &&
              (!git?.is_repo || git.dirty_count === 0));
          return (
            <label
              key={e.key}
              className={cn(
                "flex items-center gap-2 rounded border border-border bg-background/40 px-2 py-1 text-[12px]",
                disabled && "opacity-50",
              )}
            >
              <input
                type="checkbox"
                checked={evidence[e.key]}
                disabled={disabled}
                onChange={(ev) => onToggle(e.key, ev.target.checked)}
                className="h-3 w-3"
              />
              <span className="flex-1">{e.label}</span>
              {e.key === "include_git_diff" && git?.diff_truncated && (
                <Badge variant="warning">trimmed</Badge>
              )}
              {e.key === "include_git_status" && git?.is_repo && git.branch && (
                <span className="font-mono text-[10px] text-muted-foreground">
                  {git.branch}
                </span>
              )}
              {e.key === "include_changed_files" && git?.is_repo && (
                <span className="text-[10px] text-muted-foreground">
                  {git.dirty_count}
                </span>
              )}
            </label>
          );
        })}
      </div>

      <details className="mt-2">
        <summary className="cursor-pointer text-[11px] text-muted-foreground hover:text-foreground">
          Paste test / build output (optional)
        </summary>
        <div className="mt-2 space-y-2">
          <textarea
            value={testOutput}
            placeholder="Paste latest test output…"
            onChange={(e) => onChangeTest(e.target.value)}
            className="block w-full resize-y rounded-md border border-border bg-background px-2 py-1 text-[12px] outline-none placeholder:text-muted-foreground/60 focus:border-primary/60 min-h-[60px]"
          />
          <textarea
            value={buildOutput}
            placeholder="Paste latest build output…"
            onChange={(e) => onChangeBuild(e.target.value)}
            className="block w-full resize-y rounded-md border border-border bg-background px-2 py-1 text-[12px] outline-none placeholder:text-muted-foreground/60 focus:border-primary/60 min-h-[60px]"
          />
        </div>
      </details>
    </div>
  );
}

function PacketSizeRow({
  preview,
  previewing,
}: {
  preview: ReviewPreview | null;
  previewing: boolean;
}) {
  if (!preview && !previewing) return null;
  return (
    <div className="flex items-center gap-3 border-b border-border bg-muted/20 px-4 py-1.5 text-[11px] text-muted-foreground">
      {previewing ? (
        <span className="flex items-center gap-1.5">
          <Loader2 className="h-3 w-3 animate-spin" />
          Sizing packet…
        </span>
      ) : preview ? (
        <>
          <span className="font-mono">
            {(preview.byte_count / 1024).toFixed(1)} KB
          </span>
          <span className="text-muted-foreground/70">·</span>
          <span className="font-mono">
            ~{preview.estimated_tokens.toLocaleString()} tokens
          </span>
          {preview.git.is_repo && (
            <>
              <span className="text-muted-foreground/70">·</span>
              <span>
                {preview.git.dirty_count} dirty files
                {preview.git.diff_truncated && " (diff trimmed)"}
              </span>
            </>
          )}
        </>
      ) : null}
    </div>
  );
}

function SecretWarning({
  hits,
  override,
  onToggleOverride,
}: {
  hits: ReviewSecretHit[];
  override: boolean;
  onToggleOverride: () => void;
}) {
  return (
    <div className="border-b border-amber-500/40 bg-amber-500/10 px-4 py-2 text-[12px] text-amber-700 dark:text-amber-300">
      <div className="mb-1 flex items-center gap-2 font-medium">
        <ShieldAlert className="h-3.5 w-3.5" />
        Secret-like values detected — won't send unless you override.
      </div>
      <ul className="ms-5 list-disc space-y-0.5 text-[11px]">
        {hits.map((h, i) => (
          <li key={i}>
            <b>{h.label}</b> in <code className="font-mono">{h.location}</code>
          </li>
        ))}
      </ul>
      <label className="mt-2 inline-flex items-center gap-2 text-[11px]">
        <input
          type="checkbox"
          checked={override}
          onChange={onToggleOverride}
          className="h-3 w-3"
        />
        Override for this send only (will be recorded in the audit log).
      </label>
    </div>
  );
}

function QuestionRow({
  question,
  onChange,
  onSend,
  sending,
  disabled,
}: {
  question: string;
  onChange: (v: string) => void;
  onSend: () => void;
  sending: boolean;
  disabled: boolean;
}) {
  const willUseDefault = !question.trim();
  return (
    <div className="border-b border-border px-4 py-3">
      <div className="mb-1.5 flex items-center justify-between text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        <span>Your question</span>
        <span className="font-normal normal-case text-muted-foreground/70">
          optional
        </span>
      </div>
      <textarea
        value={question}
        placeholder="Optional: tell the reviewer what to focus on. Leave blank for a general review."
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
            e.preventDefault();
            onSend();
          }
        }}
        className="block w-full resize-y rounded-md border border-border bg-background px-3 py-2 text-[13px] outline-none placeholder:text-muted-foreground/60 focus:border-primary/60 min-h-[100px]"
      />
      <div className="mt-2 flex items-center gap-2">
        <Button
          onClick={onSend}
          size="sm"
          className="h-7"
          disabled={sending || disabled}
          title={
            disabled
              ? "Create or select a thread first"
              : willUseDefault
              ? "Send a general review (Cmd-Enter)"
              : "Send to reviewer (Cmd-Enter)"
          }
        >
          {sending ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Send className="h-3 w-3" />
          )}
          {sending
            ? "Sending…"
            : willUseDefault
            ? "Send (general review)"
            : "Send to reviewer"}
        </Button>
        {willUseDefault && !disabled && (
          <span className="text-[10.5px] text-muted-foreground/80">
            Will use a default review prompt.
          </span>
        )}
        <span className="ms-auto text-[10px] text-muted-foreground">
          ⌘ + Enter
        </span>
      </div>
    </div>
  );
}

function MessagesList({
  messages,
  onCopyNextPrompt,
}: {
  messages: ReviewMessage[];
  onCopyNextPrompt: (reply: string) => void;
}) {
  if (messages.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center px-4 py-8 text-[11px] text-muted-foreground">
        No exchanges yet in this thread.
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3 px-4 py-3">
      {messages.map((m) => (
        <article
          key={m.id}
          className={cn(
            "rounded-md border p-3 text-[13px] leading-relaxed",
            m.role === "reviewer"
              ? "border-primary/30 bg-primary/5"
              : "border-border bg-background/60",
          )}
        >
          <header className="mb-1.5 flex items-center gap-2 text-[10.5px] uppercase tracking-wider text-muted-foreground">
            <MessageSquare className="h-3 w-3" />
            <span>{m.role}</span>
            {m.model && (
              <span className="font-mono text-[10px]">{m.model}</span>
            )}
            <span className="ms-auto">{formatRelative(m.created_at)}</span>
            {m.estimated_tokens != null && (
              <span className="font-mono text-[10px]">
                ~{m.estimated_tokens} tk
              </span>
            )}
            {m.provider_tokens != null && (
              <span className="font-mono text-[10px]">
                {m.provider_tokens} actual
              </span>
            )}
          </header>
          <pre className="whitespace-pre-wrap font-sans text-[12.5px]">
            {m.content}
          </pre>
          {m.role === "reviewer" && (
            <div className="mt-2 flex justify-end">
              <Button
                size="sm"
                variant="outline"
                className="h-7"
                onClick={() => onCopyNextPrompt(m.content)}
                title="Copy the 'next prompt for Claude Code' section"
              >
                <Copy className="h-3 w-3" />
                Copy next prompt
              </Button>
            </div>
          )}
        </article>
      ))}
    </div>
  );
}
