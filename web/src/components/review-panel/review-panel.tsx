import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronRight,
  ClipboardCheck,
  Clock,
  Copy,
  History,
  Loader2,
  MessageSquare,
  Plus,
  Send,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useApp } from "@/store/app";
import {
  api,
  type ReviewEvidenceFlags,
  type ReviewMessage,
  type ReviewPreview,
  type ReviewSecretHit,
  type ReviewSkill,
  type ReviewThread,
  type SkillId,
} from "@/lib/api";
import { cn, formatRelative } from "@/lib/utils";
import { sessionDisplayName } from "@/lib/session-display";
import { copyTargetForReply } from "@/lib/extract-next-prompt";
import {
  parseCoachReview,
  parseCriticalReview,
  type CoachReview,
  type CriticalReview,
} from "@/lib/review-parser";
import { effectiveQuestion } from "./effective-question";
import { toast } from "sonner";

/** Fallback skill list when ``/api/reviews/skills`` hasn't replied yet (or
 *  the call failed). The runtime list is fetched on panel-open and cached
 *  in component state; the UI swaps over once it arrives. */
const FALLBACK_SKILLS: ReviewSkill[] = [
  {
    id: "quick_review",
    label: "Quick Review",
    purpose: "Fast daily review of the current Claude result.",
  },
  {
    id: "critical_review",
    label: "Critical Review",
    purpose: "Find risks, weak assumptions, missing tests, scope creep.",
  },
  {
    id: "prompt_coach",
    label: "Prompt Coach",
    purpose: "Help write the best next prompt for Claude Code.",
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
  // Auto Review: when checked, an empty composer + Send is allowed and
  // sends the DEFAULT_QUESTION. When unchecked, Send is disabled until the
  // user types guidance — the spec wants this to be an explicit opt-in for
  // "just review it for me, no guidance needed".
  const [autoReview, setAutoReview] = React.useState(false);
  // Subject card (Claude result preview at the top) and the Evidence
  // section default to collapsed — chat composer is the visual focus.
  const [subjectExpanded, setSubjectExpanded] = React.useState(false);
  // "Current review only" filtering. We capture the timestamp at which
  // ensure-thread completed for the current opening; messages older than
  // that are considered "history" and hidden from the main chat by default.
  // The user toggles ``showHistory`` to reveal them.
  const [openedAt, setOpenedAt] = React.useState<number>(() => Date.now());
  const [showHistory, setShowHistory] = React.useState(false);
  // Skill registry, fetched from /api/reviews/skills on first open. The
  // FALLBACK list keeps the pills usable even if the request hasn't
  // resolved yet (or fails) — the runtime list overrides it.
  const [skills, setSkills] = React.useState<ReviewSkill[]>(FALLBACK_SKILLS);
  const skillsFetchedRef = React.useRef(false);
  React.useEffect(() => {
    if (!open || skillsFetchedRef.current) return;
    skillsFetchedRef.current = true;
    api
      .reviewsListSkills()
      .then((r) => setSkills(r.skills))
      .catch(() => {
        // Stay on the fallback list — better than rendering an empty pill row.
      });
  }, [open]);

  const activeThread = threads.find((t) => t.id === panel.threadId) ?? null;

  // "Current review only" filtering: by default the chat shows messages
  // newer than the panel-open timestamp. Flip showHistory to see older
  // messages from the same thread.
  const visibleMessages = React.useMemo(
    () =>
      showHistory
        ? messages
        : messages.filter((m) => m.created_at >= openedAt),
    [messages, showHistory, openedAt],
  );
  const hiddenHistoryCount = messages.length - visibleMessages.length;

  // Find the most-recent reviewer message in the current view that has a
  // usable next-prompt. We hoist that prompt into a prominent box below
  // the chat so the user doesn't have to dig for it inside the message.
  const latestPrompt = React.useMemo<{ msg: ReviewMessage; prompt: string } | null>(() => {
    for (let i = visibleMessages.length - 1; i >= 0; i--) {
      const m = visibleMessages[i];
      if (m.role !== "reviewer") continue;
      const skill = reviewerModeFromMessage(m);
      if (renderModeForSkill(skill) === "critical_or_quick") {
        const p = parseCriticalReview(m.content);
        if (p.parsed && p.nextPrompt) return { msg: m, prompt: p.nextPrompt };
      } else {
        const p = parseCoachReview(m.content);
        if (p.parsed && p.improvedPrompt) return { msg: m, prompt: p.improvedPrompt };
      }
    }
    return null;
  }, [visibleMessages]);

  const composerHasText = panel.question.trim().length > 0;
  // Send is enabled iff there's text OR Auto Review is on (an explicit
  // opt-in to send a default review). The thread must also exist; setup
  // banner handles the "still preparing" case so it's safe to gate on
  // activeThread here.
  const canSend = !!activeThread && !sending && (composerHasText || autoReview);

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
      // Mark the start of THIS opening so older messages from the same
      // thread are filtered out of the default view. The user can flip
      // showHistory to see them.
      setOpenedAt(Date.now());
      setShowHistory(false);
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
      skill_id: panel.skillId,
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
      panel.skillId,
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

  /** Copies the *already-cleaned* prompt string. The caller (the
   *  individual reviewer-message view) decides what to copy: the parsed
   *  next-prompt section when the structured parser succeeded, or a
   *  best-effort fallback otherwise. We never copy the full review. */
  const onCopyPrompt = async (prompt: string) => {
    if (!prompt || !prompt.trim()) {
      toast.error("Nothing to copy yet");
      return;
    }
    try {
      await navigator.clipboard.writeText(prompt.trim());
      toast.success("Copied next prompt");
    } catch {
      toast.error("Clipboard write failed");
    }
  };

  return (
    <Sheet open={open} onOpenChange={(o) => (o ? null : close())}>
      <SheetContent
        side="right"
        className="flex h-full flex-col p-0 sm:max-w-[720px]"
      >
        <header className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-3">
          <ClipboardCheck className="h-4 w-4 text-primary" />
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold">Review current Claude work</div>
            <div className="truncate text-[11px] text-muted-foreground">
              {projectCwd ?? "No project selected"}
              {sessionMeta && (
                <>
                  <span className="mx-1.5">·</span>
                  <span className="font-mono">
                    {sessionDisplayName(sessionMeta, 32)}
                  </span>
                </>
              )}
              {panel.sourceTurnUuid && (
                <span className="ms-2 rounded bg-muted px-1 font-mono">
                  turn {panel.sourceTurnUuid.slice(0, 8)}
                </span>
              )}
            </div>
          </div>
          <HistoryPopover
            threads={threads}
            activeId={panel.threadId}
            onSelect={(id) => setField("threadId", id)}
            onCreate={onCreateThread}
            creating={creatingThread}
          />
          <Badge variant="muted">codex</Badge>
        </header>

        <SetupBanner
          state={setupState}
          error={setupError}
          onRetry={() =>
            ensureThread(panel.sourceTurnUuid, panel.sourceTurnText)
          }
        />

        {/* Chat-style scroll region: subject card + options + evidence
            collapse + the message thread, all in one column. The composer
            is pinned below this so the textbox stays put while messages
            grow upward. */}
        <div className="flex min-w-0 flex-1 flex-col overflow-y-auto scrollbar-thin">
          <SubjectCard
            text={panel.sourceTurnText}
            role={panel.sourceTurnRole}
            expanded={subjectExpanded}
            onToggle={() => setSubjectExpanded((v) => !v)}
          />

          <OptionsRow
            skillId={panel.skillId}
            skills={skills}
            onChangeSkill={(id) => setField("skillId", id)}
            autoReview={autoReview}
            onChangeAutoReview={setAutoReview}
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

          {preview?.secret_hits?.length ? (
            <SecretWarning
              hits={preview.secret_hits}
              override={secretOverride}
              onToggleOverride={() => setSecretOverride((v) => !v)}
            />
          ) : null}

          <HistoryToggle
            hiddenCount={hiddenHistoryCount}
            showing={showHistory}
            onToggle={() => setShowHistory((v) => !v)}
          />

          <MessagesList
            messages={visibleMessages}
            hidePromptBoxes
            onCopyPrompt={onCopyPrompt}
          />

          {latestPrompt && (
            <LatestPromptBox
              prompt={latestPrompt.prompt}
              modeLabel={
                reviewerModeFromMessage(latestPrompt.msg) === "prompt_coach"
                  ? "Improved prompt"
                  : "Prompt to send Claude Code"
              }
              createdAt={latestPrompt.msg.created_at}
              onCopy={() => onCopyPrompt(latestPrompt.prompt)}
            />
          )}
        </div>

        {/* Composer pinned at the bottom of the sheet. Stays visible
            regardless of how long the chat scroll grows. */}
        <Composer
          question={panel.question}
          onChange={(v) => setField("question", v)}
          onSend={onSend}
          sending={sending}
          canSend={canSend}
          autoReview={autoReview}
          onChangeAutoReview={setAutoReview}
          packetPreview={preview}
          previewing={previewing}
        />
      </SheetContent>
    </Sheet>
  );
}

// -- subcomponents --

/** Thin status banner shown above the main content while auto-setup is
 *  running, or when it fails. Replaces the sidebar's previous loading /
 *  error UI now that the sidebar is gone. */
function SetupBanner({
  state,
  error,
  onRetry,
}: {
  state: SetupState;
  error: string | null;
  onRetry: () => void;
}) {
  if (state === "loading") {
    return (
      <div className="flex items-center gap-2 border-b border-border bg-muted/30 px-4 py-1.5 text-[11px] text-muted-foreground">
        <Loader2 className="h-3 w-3 animate-spin" />
        Preparing review thread…
      </div>
    );
  }
  if (state === "error") {
    return (
      <div className="flex items-start gap-2 border-b border-destructive/40 bg-destructive/10 px-4 py-2 text-[11.5px] text-destructive">
        <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="font-medium">Couldn't prepare a review thread</div>
          <div className="mt-0.5 break-words text-[10.5px] opacity-90">
            {error ?? "Unknown error"}
          </div>
        </div>
        <Button size="sm" variant="outline" className="h-6" onClick={onRetry}>
          Retry
        </Button>
      </div>
    );
  }
  return null;
}

/** Header-anchored popover that hosts the previous-reviews list and the
 *  manual "New thread" action. Hidden behind a small "History" button so
 *  the default workflow stays single-focus on the current review. */
function HistoryPopover({
  threads,
  activeId,
  onSelect,
  onCreate,
  creating,
}: {
  threads: ReviewThread[];
  activeId: number | null;
  onSelect: (id: number) => void;
  onCreate: () => void;
  creating: boolean;
}) {
  const [open, setOpen] = React.useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 gap-1.5"
          title="Previous reviews"
        >
          <History className="h-3.5 w-3.5" />
          History
          {threads.length > 0 && (
            <span className="rounded bg-muted/70 px-1.5 font-mono text-[10px] text-muted-foreground">
              {threads.length}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-0">
        <div className="flex items-center justify-between border-b border-border px-3 py-2 text-[11px] uppercase tracking-wider text-muted-foreground">
          <span>Previous reviews</span>
          <Button
            size="sm"
            variant="outline"
            className="h-6"
            onClick={() => {
              onCreate();
              setOpen(false);
            }}
            disabled={creating}
            title="Start an additional separate review thread"
          >
            {creating ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <Plus className="h-3 w-3" />
            )}
            New thread
          </Button>
        </div>
        <div className="max-h-[360px] overflow-y-auto scrollbar-thin">
          {threads.length === 0 && (
            <div className="px-3 py-3 text-[11.5px] text-muted-foreground">
              No previous reviews yet.
            </div>
          )}
          {threads.map((t) => (
            <button
              key={t.id}
              onClick={() => {
                onSelect(t.id);
                setOpen(false);
              }}
              className={cn(
                "flex w-full items-start gap-2 px-3 py-2 text-left text-[12px] transition-colors hover:bg-accent/50",
                activeId === t.id && "bg-accent/70",
              )}
              title={t.name}
            >
              <span
                className={cn(
                  "mt-0.5 inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center",
                  activeId === t.id ? "text-primary" : "opacity-0",
                )}
                aria-hidden={activeId !== t.id}
              >
                <Check className="h-3 w-3" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{t.name}</span>
                <span className="text-[10px] text-muted-foreground">
                  {formatRelative(t.updated_at)}
                  {t.provider_session_id && (
                    <span className="ms-1 font-mono opacity-60">· resume</span>
                  )}
                </span>
              </span>
            </button>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** Subject card: shows the Claude result that's being reviewed. Collapsed
 *  by default — first 4 lines + a "show more" toggle. The card is part of
 *  the scroll region so it scrolls out of view as the chat grows. */
function SubjectCard({
  text,
  role,
  expanded,
  onToggle,
}: {
  text: string | null;
  role: string | null;
  expanded: boolean;
  onToggle: () => void;
}) {
  if (!text || !text.trim()) {
    return (
      <div className="border-b border-border bg-muted/30 px-4 py-2.5 text-[11.5px] text-muted-foreground">
        No specific Claude result selected. The reviewer will use whatever
        evidence you toggle on below.
      </div>
    );
  }
  // Collapsed preview: first ~4 lines, capped at ~280 chars so a wide
  // single-line result still gives a peek without dominating the panel.
  const collapsedPreviewLines = 4;
  const lines = text.split(/\r?\n/);
  const isLong = lines.length > collapsedPreviewLines || text.length > 280;
  const collapsedText = isLong
    ? lines.slice(0, collapsedPreviewLines).join("\n").slice(0, 280) + "…"
    : text;
  return (
    <div className="border-b border-border bg-muted/20 px-4 py-3">
      <div className="mb-1.5 flex items-center justify-between">
        <div className="flex items-center gap-1.5 text-[10.5px] font-medium uppercase tracking-wider text-muted-foreground">
          <Sparkles className="h-3 w-3" />
          Reviewing this Claude result
          {role && <span className="font-mono normal-case">· {role}</span>}
        </div>
        {isLong && (
          <button
            type="button"
            onClick={onToggle}
            className="flex items-center gap-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
          >
            {expanded ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            {expanded ? "Show less" : "Show more"}
          </button>
        )}
      </div>
      <pre
        className={cn(
          "whitespace-pre-wrap font-sans text-[12.5px] leading-relaxed text-foreground/90",
          !expanded && isLong && "max-h-[120px] overflow-hidden",
        )}
      >
        {expanded ? text : collapsedText}
      </pre>
    </div>
  );
}

/** Compact options row: reviewer-mode pills + Auto Review checkbox. The
 *  codex provider badge already lives in the header, so we keep this row
 *  short and inline rather than a full mode-card grid. */
function OptionsRow({
  skillId,
  skills,
  onChangeSkill,
  autoReview,
  onChangeAutoReview,
}: {
  skillId: SkillId;
  skills: ReviewSkill[];
  onChangeSkill: (s: SkillId) => void;
  autoReview: boolean;
  onChangeAutoReview: (v: boolean) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2">
      <span className="text-[10.5px] font-medium uppercase tracking-wider text-muted-foreground">
        Review skill
      </span>
      <div className="flex flex-wrap items-center gap-1.5">
        {skills.map((s) => (
          <button
            key={s.id}
            type="button"
            onClick={() => onChangeSkill(s.id)}
            title={s.purpose}
            className={cn(
              "rounded-md border px-2 py-1 text-[11.5px] transition-colors",
              skillId === s.id
                ? "border-primary bg-primary/10 text-foreground"
                : "border-border text-muted-foreground hover:bg-accent/40",
            )}
          >
            {s.label}
          </button>
        ))}
      </div>
      <label
        className="ms-auto flex cursor-pointer items-center gap-1.5 text-[11.5px] text-muted-foreground"
        title="If checked, sending an empty composer runs a default review of the current evidence."
      >
        <input
          type="checkbox"
          checked={autoReview}
          onChange={(e) => onChangeAutoReview(e.target.checked)}
          className="h-3 w-3"
        />
        <span>Auto review this result</span>
      </label>
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
  // Default-collapsed: the question + reviewer reply should be the
  // visual focus. The user can expand evidence when they want to
  // change defaults; once expanded, it stays so for the session.
  const [open, setOpen] = React.useState(false);
  // Summarize what's enabled so the collapsed header still tells the
  // user what they're about to send.
  const enabled = (Object.keys(evidence) as (keyof ReviewEvidenceFlags)[]).filter(
    (k) => evidence[k],
  );
  return (
    <div className="border-b border-border px-4 py-2.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground transition-colors hover:text-foreground"
      >
        {open ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        <span>Evidence and technical context</span>
        <span className="font-mono text-[10px] normal-case text-muted-foreground/70">
          {enabled.length}/{EVIDENCE_LABELS.length} on
        </span>
      </button>

      {open && (
        <>
          <div className="mt-2 grid grid-cols-2 gap-1.5">
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
                  {e.key === "include_git_status" &&
                    git?.is_repo &&
                    git.branch && (
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

          {/* Test / build paste areas appear only when their toggle is on,
              per spec. The user enables the toggle first, then pastes. */}
          {evidence.include_test_output && (
            <textarea
              value={testOutput}
              placeholder="Paste latest test output…"
              onChange={(e) => onChangeTest(e.target.value)}
              className="mt-2 block w-full resize-y rounded-md border border-border bg-background px-2 py-1 text-[12px] outline-none placeholder:text-muted-foreground/60 focus:border-primary/60 min-h-[60px]"
            />
          )}
          {evidence.include_build_output && (
            <textarea
              value={buildOutput}
              placeholder="Paste latest build output…"
              onChange={(e) => onChangeBuild(e.target.value)}
              className="mt-2 block w-full resize-y rounded-md border border-border bg-background px-2 py-1 text-[12px] outline-none placeholder:text-muted-foreground/60 focus:border-primary/60 min-h-[60px]"
            />
          )}
        </>
      )}
    </div>
  );
}

// PacketSizeRow was a standalone bar between Evidence and the question
// textarea; it's now replaced by the inline PacketSizePill living next to
// the Send button in the composer.

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

/** Bottom-pinned chat composer. Stays visible regardless of how long the
 *  message scroll grows. Send is enabled when the textbox has text OR
 *  Auto Review is checked (an explicit opt-in to send the default review
 *  with no guidance). The packet size/token estimate is shown inline so
 *  the user knows what they're about to send. */
function Composer({
  question,
  onChange,
  onSend,
  sending,
  canSend,
  autoReview,
  onChangeAutoReview,
  packetPreview,
  previewing,
}: {
  question: string;
  onChange: (v: string) => void;
  onSend: () => void;
  sending: boolean;
  canSend: boolean;
  autoReview: boolean;
  onChangeAutoReview: (v: boolean) => void;
  packetPreview: ReviewPreview | null;
  previewing: boolean;
}) {
  const willUseDefault = !question.trim();
  return (
    <div className="shrink-0 border-t border-border bg-card/40 px-4 pb-3 pt-2">
      <textarea
        value={question}
        placeholder="Guide the reviewer. Example: Focus on architecture risk and help me write the next Claude prompt."
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
            e.preventDefault();
            if (canSend) onSend();
          }
        }}
        className="block w-full resize-y rounded-md border border-border bg-background px-3 py-2 text-[13px] outline-none placeholder:text-muted-foreground/60 focus:border-primary/60 min-h-[80px]"
      />
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <label
          className="flex cursor-pointer items-center gap-1.5 text-[11.5px] text-muted-foreground"
          title="If checked, sending an empty composer runs a default review of the current evidence."
        >
          <input
            type="checkbox"
            checked={autoReview}
            onChange={(e) => onChangeAutoReview(e.target.checked)}
            className="h-3 w-3"
          />
          <span>Auto review this result</span>
        </label>
        <PacketSizePill preview={packetPreview} previewing={previewing} />
        <Button
          onClick={onSend}
          size="sm"
          className="ms-auto h-7"
          disabled={!canSend}
          title={
            !canSend
              ? willUseDefault && !autoReview
                ? "Type guidance, or check Auto Review to send a default review."
                : "Send to reviewer"
              : willUseDefault
              ? "Send a default review (Cmd-Enter)"
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
            : willUseDefault && autoReview
            ? "Send (default review)"
            : "Send"}
        </Button>
      </div>
    </div>
  );
}

/** Inline packet-size hint shown next to the Send button. Quietly lets the
 *  user see the size estimate without giving the evidence section a whole
 *  status row of its own. */
function PacketSizePill({
  preview,
  previewing,
}: {
  preview: ReviewPreview | null;
  previewing: boolean;
}) {
  if (previewing && !preview) {
    return (
      <span className="flex items-center gap-1 text-[11px] text-muted-foreground/80">
        <Loader2 className="h-3 w-3 animate-spin" />
        sizing…
      </span>
    );
  }
  if (!preview) return null;
  return (
    <span className="text-[11px] text-muted-foreground/80">
      ~{preview.estimated_tokens.toLocaleString()} tokens
      {preview.git.is_repo && preview.git.diff_truncated && " · diff trimmed"}
    </span>
  );
}

function MessagesList({
  messages,
  onCopyPrompt,
  hidePromptBoxes,
}: {
  messages: ReviewMessage[];
  /** Copies the cleaned prompt string. Caller decides what to copy. */
  onCopyPrompt: (prompt: string) => void;
  /** When true, the per-message reviewer card omits its own prompt box.
   *  The panel hoists the latest prompt into a separate prominent
   *  LatestPromptBox below the message list, so duplicating it inside
   *  the reviewer card would be noisy. */
  hidePromptBoxes?: boolean;
}) {
  if (messages.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center px-4 py-8 text-[11px] text-muted-foreground">
        Send a message to start the review.
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3 px-4 py-3">
      {messages.map((m) =>
        m.role === "reviewer" ? (
          <ReviewerMessageView
            key={m.id}
            msg={m}
            onCopyPrompt={onCopyPrompt}
            hidePromptBox={hidePromptBoxes}
          />
        ) : (
          <UserMessageView key={m.id} msg={m} />
        ),
      )}
    </div>
  );
}

/** Toggle row for "Show / Hide history" — only renders when there are
 *  messages older than the panel-open timestamp. Lives between the
 *  evidence section and the messages list so it's discoverable but not
 *  noisy. */
function HistoryToggle({
  hiddenCount,
  showing,
  onToggle,
}: {
  hiddenCount: number;
  showing: boolean;
  onToggle: () => void;
}) {
  // Always render when showing is true (so the user can fold history back
  // away), or when there are hidden messages to reveal.
  if (!showing && hiddenCount <= 0) return null;
  return (
    <div className="flex items-center justify-between border-b border-dashed border-border bg-muted/20 px-4 py-1.5 text-[11px] text-muted-foreground">
      <span className="flex items-center gap-1.5">
        <Clock className="h-3 w-3" />
        {showing
          ? "Showing full review history"
          : hiddenCount === 1
          ? "1 earlier message hidden"
          : `${hiddenCount} earlier messages hidden`}
      </span>
      <button
        type="button"
        onClick={onToggle}
        className="font-medium text-primary underline-offset-2 hover:underline"
      >
        {showing ? "Hide history" : "Show history"}
      </button>
    </div>
  );
}

/** The user's question — kept compact, secondary to the reviewer reply. */
function UserMessageView({ msg }: { msg: ReviewMessage }) {
  return (
    <article className="rounded-md border border-border bg-background/60 p-3 text-[13px] leading-relaxed">
      <header className="mb-1.5 flex items-center gap-2 text-[10.5px] uppercase tracking-wider text-muted-foreground">
        <MessageSquare className="h-3 w-3" />
        <span>you</span>
        <span className="ms-auto">{formatRelative(msg.created_at)}</span>
        {msg.estimated_tokens != null && (
          <span className="font-mono text-[10px]">
            ~{msg.estimated_tokens} tk
          </span>
        )}
      </header>
      <pre className="whitespace-pre-wrap font-sans text-[12.5px]">
        {msg.content}
      </pre>
    </article>
  );
}

/** Pull the skill id that was used when this message was sent. Reads the
 *  new ``skill_id`` field first, then the legacy ``reviewer_mode`` for
 *  back-compat with messages from before the skill system, then falls
 *  back to ``critical_review`` (the default for ambiguous legacy data).
 *  Returns the skill_id; the caller maps it to a render mode. */
function reviewerModeFromMessage(m: ReviewMessage): SkillId {
  const ctx = m.context_used_json as
    | { skill_id?: string; reviewer_mode?: string }
    | null;
  const skill = ctx?.skill_id;
  if (
    skill === "quick_review" ||
    skill === "critical_review" ||
    skill === "prompt_coach"
  ) {
    return skill;
  }
  const legacy = ctx?.reviewer_mode;
  if (legacy === "quick_review") return "quick_review";
  if (legacy === "prompt_coach") return "prompt_coach";
  if (legacy === "critical_review" || legacy === "critical") {
    return "critical_review";
  }
  return "critical_review";
}

/** Resolve the render mode for a message — Critical and Quick Review
 *  share the same compact verdict/why/next/prompt view; Prompt Coach has
 *  its own. */
function renderModeForSkill(
  skill: SkillId,
): "critical_or_quick" | "prompt_coach" {
  return skill === "prompt_coach" ? "prompt_coach" : "critical_or_quick";
}

function ReviewerMessageView({
  msg,
  onCopyPrompt,
  hidePromptBox,
}: {
  msg: ReviewMessage;
  onCopyPrompt: (prompt: string) => void;
  hidePromptBox?: boolean;
}) {
  const skill = reviewerModeFromMessage(msg);
  const renderMode = renderModeForSkill(skill);
  const parsed = React.useMemo(
    () =>
      renderMode === "critical_or_quick"
        ? parseCriticalReview(msg.content)
        : parseCoachReview(msg.content),
    [msg.content, renderMode],
  );

  // Short label shown in the message header — Quick / Critical / Coach.
  const skillLabel =
    skill === "quick_review"
      ? "quick"
      : skill === "prompt_coach"
      ? "coach"
      : "critical";

  return (
    <article className="rounded-lg border border-primary/30 bg-primary/5 p-4">
      <header className="mb-3 flex items-center gap-2 text-[10.5px] uppercase tracking-wider text-muted-foreground">
        <ShieldCheck className="h-3 w-3 text-primary" />
        <span>reviewer · {skillLabel}</span>
        {msg.model && (
          <span className="font-mono text-[10px]">{msg.model}</span>
        )}
        <span className="ms-auto">{formatRelative(msg.created_at)}</span>
        {msg.provider_tokens != null && (
          <span className="font-mono text-[10px]">
            {msg.provider_tokens} tk
          </span>
        )}
      </header>

      {parsed.parsed && renderMode === "critical_or_quick" ? (
        <CriticalReviewView
          parsed={parsed as CriticalReview}
          fullContent={msg.content}
          onCopyPrompt={onCopyPrompt}
          hidePromptBox={hidePromptBox}
        />
      ) : parsed.parsed && renderMode === "prompt_coach" ? (
        <CoachReviewView
          parsed={parsed as CoachReview}
          fullContent={msg.content}
          onCopyPrompt={onCopyPrompt}
          hidePromptBox={hidePromptBox}
        />
      ) : (
        <RawReviewerView msg={msg} onCopyPrompt={onCopyPrompt} />
      )}
    </article>
  );
}

// ---------------------------------------------------------------------------
// Critical Reviewer compact view
// ---------------------------------------------------------------------------

function CriticalReviewView({
  parsed,
  fullContent,
  onCopyPrompt,
  hidePromptBox,
}: {
  parsed: CriticalReview;
  fullContent: string;
  onCopyPrompt: (prompt: string) => void;
  /** When true, the prompt is rendered separately below the message
   *  list (LatestPromptBox) so we omit it here to avoid duplication. */
  hidePromptBox?: boolean;
}) {
  return (
    <div className="space-y-3 text-[13px] leading-relaxed">
      {parsed.verdict && (
        <p className="text-[14px] font-medium text-foreground">
          {parsed.verdict}
        </p>
      )}

      {parsed.why.length > 0 && (
        <ul className="space-y-1 text-foreground/90">
          {parsed.why.slice(0, 3).map((f, i) => (
            <li key={i} className="flex gap-2">
              <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-foreground/60" />
              <span>{f}</span>
            </li>
          ))}
        </ul>
      )}

      {parsed.nextAction && (
        <p className="text-foreground/90">
          <span className="font-semibold text-foreground">Next: </span>
          {parsed.nextAction}
        </p>
      )}

      {!hidePromptBox &&
        (parsed.nextPrompt ? (
          <NextPromptBox
            label="Prompt to send Claude"
            prompt={parsed.nextPrompt}
            onCopy={() => onCopyPrompt(parsed.nextPrompt!)}
          />
        ) : (
          <NoPromptHint />
        ))}

      {parsed.details && <CollapsibleDetails text={parsed.details} />}
      <ShowFullReviewToggle content={fullContent} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Prompt Coach compact view
// ---------------------------------------------------------------------------

function CoachReviewView({
  parsed,
  fullContent,
  onCopyPrompt,
  hidePromptBox,
}: {
  parsed: CoachReview;
  fullContent: string;
  onCopyPrompt: (prompt: string) => void;
  hidePromptBox?: boolean;
}) {
  return (
    <div className="space-y-3 text-[13px] leading-relaxed">
      {parsed.clarifiedIntent && (
        <Section title="Clarified intent">
          <p className="text-foreground/90">{parsed.clarifiedIntent}</p>
        </Section>
      )}

      {!hidePromptBox &&
        (parsed.improvedPrompt ? (
          <NextPromptBox
            label="Improved prompt"
            prompt={parsed.improvedPrompt}
            onCopy={() => onCopyPrompt(parsed.improvedPrompt!)}
          />
        ) : (
          <NoPromptHint />
        ))}

      {parsed.whyThisIsBetter.length > 0 && (
        <Section title="Why this is better">
          <ul className="space-y-1">
            {parsed.whyThisIsBetter.slice(0, 3).map((r, i) => (
              <li key={i} className="flex gap-2">
                <span className="mt-[6px] h-1 w-1 shrink-0 rounded-full bg-foreground/60" />
                <span>{r}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {parsed.details && <CollapsibleDetails text={parsed.details} />}
      <ShowFullReviewToggle content={fullContent} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Raw fallback when parsing fails
// ---------------------------------------------------------------------------

function RawReviewerView({
  msg,
  onCopyPrompt,
}: {
  msg: ReviewMessage;
  onCopyPrompt: (prompt: string) => void;
}) {
  // Even when the parser bailed, the model may still have written one of
  // the recognized prompt headings. The strict extractor returns null
  // when no heading is present — we never copy a "best guess" of the
  // whole reply.
  const extracted = copyTargetForReply(msg.content);
  return (
    <div className="space-y-3 text-[13px] leading-relaxed">
      <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[11px] text-amber-700 dark:text-amber-300">
        Couldn't parse a structured response — showing the raw reply below.
      </div>
      <pre className="whitespace-pre-wrap font-sans text-[12.5px]">
        {msg.content}
      </pre>
      {extracted ? (
        <NextPromptBox
          label="Prompt to send Claude"
          prompt={extracted}
          onCopy={() => onCopyPrompt(extracted)}
        />
      ) : (
        <NoPromptHint />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components shared between Critical / Coach views
// ---------------------------------------------------------------------------

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h3 className="mb-1 text-[10.5px] font-semibold uppercase tracking-wider text-muted-foreground">
        {title}
      </h3>
      <div>{children}</div>
    </section>
  );
}

/** Inline hint shown when the reviewer didn't include a copy-ready
 *  prompt section. Per spec, we don't copy a "best guess" of the whole
 *  reply — we tell the user nothing was extracted instead. */
function NoPromptHint() {
  return (
    <div className="rounded-md border border-dashed border-border bg-muted/30 px-3 py-2 text-[11.5px] text-muted-foreground">
      No prompt found yet — ask the reviewer to write one (or try a
      follow-up like "give me the prompt to send Claude").
    </div>
  );
}

/** Inline collapsible "Details" section the model writes only when the
 *  user explicitly asked for deeper analysis. Default-collapsed so the
 *  conversational view stays brief. */
function CollapsibleDetails({ text }: { text: string }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
      >
        {open ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        {open ? "Hide details" : "More details"}
      </button>
      {open && (
        <p className="mt-1.5 text-foreground/85 whitespace-pre-wrap">
          {text}
        </p>
      )}
    </div>
  );
}

function NextPromptBox({
  label,
  prompt,
  onCopy,
}: {
  label: string;
  prompt: string;
  onCopy: () => void;
}) {
  return (
    <section className="rounded-md border border-primary/40 bg-background p-3">
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <h3 className="text-[10.5px] font-semibold uppercase tracking-wider text-primary">
          {label}
        </h3>
        <Button
          size="sm"
          variant="outline"
          className="h-7"
          onClick={onCopy}
          title="Copy this prompt to the clipboard (no fences, no preamble)"
        >
          <Copy className="h-3 w-3" />
          Copy next prompt
        </Button>
      </div>
      <PromptMarkdown text={prompt} />
    </section>
  );
}

/** Renders a Markdown-formatted prompt: bullets, numbered lists, and
 *  fenced code blocks come through as proper visuals; plain prose stays
 *  legible. The wrapper class keeps it visually anchored as a "prompt
 *  block" with light typographic spacing.
 *
 *  We DON'T strip outer fences here — the parser already did that via
 *  ``stripFences`` so the prompt is clean by the time it reaches us. */
function PromptMarkdown({ text }: { text: string }) {
  return (
    <div className="prompt-markdown text-[13px] leading-relaxed text-foreground">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Tight spacing — matches the existing chat density.
          p: ({ children, ...props }) => (
            <p {...props} className="my-1.5 whitespace-pre-wrap">
              {children}
            </p>
          ),
          ul: ({ children, ...props }) => (
            <ul {...props} className="my-1.5 ms-5 list-disc space-y-0.5">
              {children}
            </ul>
          ),
          ol: ({ children, ...props }) => (
            <ol {...props} className="my-1.5 ms-5 list-decimal space-y-0.5">
              {children}
            </ol>
          ),
          li: ({ children, ...props }) => (
            <li {...props} className="leading-relaxed">
              {children}
            </li>
          ),
          code: ({ children, ...props }) => {
            const inline = !(props as any).className?.includes("language-");
            if (inline) {
              return (
                <code
                  className="rounded bg-muted px-1 py-0.5 font-mono text-[12px]"
                >
                  {children}
                </code>
              );
            }
            return (
              <code className="font-mono text-[12px]">{children}</code>
            );
          },
          pre: ({ children, ...props }) => (
            <pre
              {...props}
              className="my-2 overflow-x-auto rounded-md bg-muted/60 p-2.5 font-mono text-[12px] leading-relaxed scrollbar-thin"
            >
              {children}
            </pre>
          ),
          h1: ({ children, ...props }) => (
            <h3 {...props} className="mt-2 mb-1 text-[14px] font-semibold">
              {children}
            </h3>
          ),
          h2: ({ children, ...props }) => (
            <h4 {...props} className="mt-2 mb-1 text-[13px] font-semibold">
              {children}
            </h4>
          ),
          h3: ({ children, ...props }) => (
            <h5 {...props} className="mt-1.5 mb-1 text-[12.5px] font-semibold">
              {children}
            </h5>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

/** Hoisted "first-class" prompt box rendered below the message list and
 *  above the composer. Pulls the latest reviewer's parsed nextPrompt
 *  (Critical) or improvedPrompt (Coach) so the user always sees the
 *  copy-ready prompt without scrolling into a reviewer card. */
function LatestPromptBox({
  prompt,
  modeLabel,
  createdAt,
  onCopy,
}: {
  prompt: string;
  modeLabel: string;
  createdAt: number;
  onCopy: () => void;
}) {
  return (
    <section className="mx-4 mb-3 rounded-lg border-2 border-primary/50 bg-background shadow-sm">
      <header className="flex items-center justify-between gap-2 border-b border-primary/20 bg-primary/5 px-3 py-2">
        <h3 className="flex items-center gap-1.5 text-[11.5px] font-semibold uppercase tracking-wider text-primary">
          <Sparkles className="h-3.5 w-3.5" />
          {modeLabel}
        </h3>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-muted-foreground">
            {formatRelative(createdAt)}
          </span>
          <Button
            size="sm"
            variant="outline"
            className="h-7"
            onClick={onCopy}
            title="Copy this prompt to the clipboard (no fences, no preamble)"
          >
            <Copy className="h-3 w-3" />
            Copy prompt
          </Button>
        </div>
      </header>
      <div className="px-3 py-2.5">
        <PromptMarkdown text={prompt} />
      </div>
    </section>
  );
}

// FallbackCopyRow was a "best-guess copy" fallback; per the chat-UX spec
// we never copy a guess of the whole reply. RawReviewerView now uses
// NextPromptBox if a heading was extractable, NoPromptHint otherwise.

function ShowFullReviewToggle({ content }: { content: string }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div className="border-t border-border/60 pt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
      >
        {open ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        {open ? "Hide full review" : "Show full review"}
      </button>
      {open && (
        <pre className="mt-2 max-h-[420px] overflow-auto whitespace-pre-wrap rounded border border-border/60 bg-muted/30 p-2 font-sans text-[12px] leading-relaxed text-foreground/90 scrollbar-thin">
          {content}
        </pre>
      )}
    </div>
  );
}
