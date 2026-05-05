import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Sparkles,
  Loader2,
  Copy,
  Check,
  Save,
  RotateCw,
  Trash2,
  Maximize2,
  Minimize2,
  Pencil,
  ChevronDown,
  ChevronRight,
  X,
  Eye,
  EyeOff,
  Wand2,
  History,
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
import { api, type PromptDraft, type PromptWriterContextMode, type PromptWriterMode } from "@/lib/api";
import {
  buildContextPack,
  decideAutoMode,
  estimateTokens,
  LARGE_CONTEXT_THRESHOLD,
  modeRequiresConfirmation,
  packCharCount,
  resolveContextMode,
} from "@/lib/context-pack";
import { AlertTriangle, Sparkle } from "lucide-react";
import { cn, formatRelative } from "@/lib/utils";
import { toast } from "sonner";

const MODE_LABELS: Record<PromptWriterMode, { label: string; hint: string }> = {
  improve: { label: "Improve Prompt", hint: "Polish wording, fix grammar, keep intent" },
  clarify: { label: "Clarify My Idea", hint: "Identify gaps, add bracketed questions" },
  developer_task: { label: "Developer Task", hint: "Structured prompt for Claude Code/Codex" },
  bug_report: { label: "Bug Report", hint: "Observed/Expected/Repro/Fix/Verify" },
  design: { label: "Design Prompt", hint: "Layout, flow, components, states" },
  critical_review: { label: "Adversarial Review", hint: "Ask AI to challenge assumptions" },
  short_command: { label: "Short Command", hint: "1–3 sentences, ready to paste" },
  continue: { label: "Continue Conversation", hint: "Treat as next message in this session" },
};

// Visible chips, in display order. Auto first, full_session last.
const CONTEXT_CHIPS: { mode: PromptWriterContextMode; label: string; hint?: string }[] = [
  { mode: "auto", label: "Auto", hint: "Pick the smallest context that fits" },
  { mode: "none", label: "No Context" },
  { mode: "current_item", label: "Current Item" },
  { mode: "selected", label: "Selected Only" },
  { mode: "recent_small", label: "Recent (3)" },
  { mode: "focused", label: "Focused" },
  { mode: "expanded", label: "Expanded" },
  { mode: "full_session", label: "Full Session", hint: "Confirmation required" },
];
// For legacy drafts, render their old name unless they map to a new chip.
const LEGACY_LABELS: Partial<Record<PromptWriterContextMode, string>> = {
  recent: "Recent (legacy)",
  selected_plus_nearby: "Selected+Nearby (legacy)",
  summary: "Summary (legacy)",
  full_feature: "Feature (legacy)",
};

export function PromptWriter() {
  const session = useApp((s) => s.session);
  const summary = useApp((s) => s.summary);
  const pw = useApp((s) => s.promptWriter);
  const close = useApp((s) => s.closePromptWriter);
  const setField = useApp((s) => s.setPromptWriterField);
  const toggleExcluded = useApp((s) => s.togglePromptItemExcluded);

  const [draft, setDraft] = React.useState<PromptDraft | null>(null);
  const [busy, setBusy] = React.useState<"" | "generate" | "refine">("");
  const [refineText, setRefineText] = React.useState("");
  const [editing, setEditing] = React.useState(false);
  const [editBuf, setEditBuf] = React.useState("");
  const [contextOpen, setContextOpen] = React.useState(true);
  const [historyOpen, setHistoryOpen] = React.useState(false);
  const [history, setHistory] = React.useState<PromptDraft[]>([]);
  const [copied, setCopied] = React.useState(false);

  // Auto-mode decision based on entry point + rough input + writer mode
  const autoDecision = React.useMemo(
    () =>
      decideAutoMode({
        hasSelection: !!pw.selectedText?.trim(),
        hasSourceEvent: !!pw.sourceEventUuid,
        roughInput: pw.rough,
        writerMode: pw.mode,
      }),
    [pw.selectedText, pw.sourceEventUuid, pw.rough, pw.mode]
  );
  const { effective: effectiveMode, reason: contextReason } = React.useMemo(
    () => resolveContextMode(pw.contextMode, autoDecision),
    [pw.contextMode, autoDecision]
  );

  const allItems = React.useMemo(() => {
    if (!session) return [];
    return buildContextPack({
      session,
      contextMode: pw.contextMode,
      selectedText: pw.selectedText ?? undefined,
      sourceEventUuid: pw.sourceEventUuid ?? undefined,
      summary:
        summary && summary.sessionId === session.meta.session_id ? summary.text : null,
      roughInput: pw.rough,
      writerMode: pw.mode,
    });
  }, [
    session,
    pw.contextMode,
    pw.selectedText,
    pw.sourceEventUuid,
    pw.rough,
    pw.mode,
    summary,
  ]);

  const items = React.useMemo(
    () => allItems.filter((it) => !pw.excludedItemIds.includes(it.id)),
    [allItems, pw.excludedItemIds]
  );
  const totalChars = packCharCount(items);

  const loadHistory = React.useCallback(async () => {
    if (!session) return;
    try {
      const list = await api.promptWriterList(session.meta.session_id, 30);
      setHistory(list);
    } catch {}
  }, [session]);

  React.useEffect(() => {
    if (pw.open) loadHistory();
  }, [pw.open, loadHistory]);

  // Reset draft when key inputs change a lot
  React.useEffect(() => {
    setDraft(null);
    setEditing(false);
    setRefineText("");
  }, [pw.sourceEventUuid, pw.selectedText, session?.meta.session_id]);

  const onGenerate = React.useCallback(async () => {
    if (!session) return;
    if (!pw.rough.trim() && items.length === 0) {
      toast.error("Type something or include some context first.");
      return;
    }
    // Confirm before sending Full Session
    if (
      modeRequiresConfirmation(pw.contextMode) ||
      modeRequiresConfirmation(effectiveMode)
    ) {
      const ok = window.confirm(
        `You're about to send the FULL SESSION (${(packCharCount(items) / 1024).toFixed(
          1
        )} KB / ~${estimateTokens(packCharCount(items))} tokens) to the model.\n\nThis is large and expensive.\n\nContinue?`
      );
      if (!ok) return;
    }
    setBusy("generate");
    try {
      const d = await api.promptWriterGenerate({
        bucket: session.meta.bucket,
        session_id: session.meta.session_id,
        source_event_uuid: pw.sourceEventUuid ?? undefined,
        mode: pw.mode,
        // Send the EFFECTIVE mode the auto-resolver picked, so the draft
        // history is honest about what was actually sent.
        context_mode: effectiveMode,
        rough_input: pw.rough,
        context_pack: items,
      });
      setDraft(d);
      setField("currentDraftId", d.id);
      setEditing(false);
      loadHistory();
      toast.success("Prompt generated");
    } catch (e: any) {
      toast.error(e?.message ?? "Generate failed");
    } finally {
      setBusy("");
    }
  }, [
    session,
    pw.rough,
    pw.sourceEventUuid,
    pw.mode,
    pw.contextMode,
    effectiveMode,
    items,
    setField,
    loadHistory,
  ]);

  const onRefine = async () => {
    if (!draft || !refineText.trim()) return;
    setBusy("refine");
    try {
      const d = await api.promptWriterRefine({
        draft_id: draft.id,
        refinement: refineText,
      });
      setDraft(d);
      setRefineText("");
      toast.success("Prompt refined");
    } catch (e: any) {
      toast.error(e?.message ?? "Refine failed");
    } finally {
      setBusy("");
    }
  };

  const onSaveEdit = async () => {
    if (!draft) return;
    try {
      const d = await api.promptWriterPatch(draft.id, { generated_prompt: editBuf });
      setDraft(d);
      setEditing(false);
      toast.success("Saved");
    } catch (e: any) {
      toast.error(e?.message ?? "Save failed");
    }
  };

  const onCopy = async () => {
    if (!draft) return;
    try {
      await navigator.clipboard.writeText(draft.generated_prompt);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
      toast.success("Copied");
    } catch {
      toast.error("Clipboard write failed");
    }
  };

  const onDelete = async () => {
    if (!draft) return;
    if (!confirm("Delete this draft?")) return;
    try {
      await api.promptWriterDelete(draft.id);
      setDraft(null);
      setField("currentDraftId", null);
      loadHistory();
    } catch {}
  };

  const onLoadHistoryItem = (d: PromptDraft) => {
    setDraft(d);
    setField("rough", d.rough_input);
    setField("mode", d.mode);
    setField("contextMode", d.context_mode);
    setField("currentDraftId", d.id);
    setHistoryOpen(false);
  };

  // Cmd-Enter to generate
  React.useEffect(() => {
    if (!pw.open) return;
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        onGenerate();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pw.open, onGenerate]);

  if (!session) return null;

  const widthClass = pw.expanded ? "sm:max-w-[1100px]" : "sm:max-w-[640px]";

  return (
    <Sheet open={pw.open} onOpenChange={(o) => (o ? null : close())}>
      <SheetContent className={cn("flex h-full flex-col p-0", widthClass)} side="right">
        {/* Header */}
        <div className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-3">
          <Wand2 className="h-4 w-4 text-primary" />
          <div className="flex-1">
            <div className="text-sm font-semibold">Context-Aware Prompt Writer</div>
            <div className="text-[11px] text-muted-foreground">
              {session.meta.cwd?.split(/[\\/]/).filter(Boolean).pop() ?? "session"} ·{" "}
              {pw.sourceEventUuid ? "from selected turn" : "session-wide"}
            </div>
          </div>
          <Popover open={historyOpen} onOpenChange={setHistoryOpen}>
            <PopoverTrigger asChild>
              <Button variant="ghost" size="icon" className="h-7 w-7" title="Drafts history">
                <History className="h-3.5 w-3.5" />
              </Button>
            </PopoverTrigger>
            <PopoverContent align="end" className="w-80 p-1 max-h-96 overflow-auto">
              {history.length === 0 && (
                <div className="p-3 text-center text-xs text-muted-foreground">
                  No drafts yet.
                </div>
              )}
              {history.map((d) => (
                <button
                  key={d.id}
                  onClick={() => onLoadHistoryItem(d)}
                  className="group flex w-full items-start gap-2 rounded px-2 py-1.5 text-left hover:bg-accent"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5 text-[11px]">
                      <Badge variant="muted" className="font-mono">
                        {MODE_LABELS[d.mode]?.label ?? d.mode}
                      </Badge>
                      <span className="text-muted-foreground">
                        {formatRelative(d.created_at)}
                      </span>
                    </div>
                    <div className="mt-1 truncate text-[12px]">
                      {d.rough_input.slice(0, 80) || "(no rough input)"}
                    </div>
                  </div>
                </button>
              ))}
            </PopoverContent>
          </Popover>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            onClick={() => setField("expanded", !pw.expanded)}
            title={pw.expanded ? "Collapse" : "Expand"}
          >
            {pw.expanded ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
          </Button>
        </div>

        {/* Body — scrollable */}
        <div className="flex-1 overflow-auto scrollbar-thin">
          {/* Context Window */}
          <div className="border-b border-border px-4 py-3">
            <button
              onClick={() => setContextOpen((o) => !o)}
              className="mb-2 flex w-full items-center gap-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground hover:text-foreground"
            >
              {contextOpen ? (
                <ChevronDown className="h-3 w-3" />
              ) : (
                <ChevronRight className="h-3 w-3" />
              )}
              Context Window
              <Badge variant="muted" className="ms-auto font-mono normal-case tracking-normal">
                {(totalChars / 1024).toFixed(1)} KB · ~{estimateTokens(totalChars)} tok
              </Badge>
            </button>
            {contextOpen && (
              <div className="space-y-1.5">
                {/* Context mode chips */}
                <div className="flex flex-wrap items-center gap-1">
                  <span className="text-[11px] text-muted-foreground">Mode:</span>
                  {CONTEXT_CHIPS.map((c) => {
                    const active = pw.contextMode === c.mode;
                    const isAutoChip = c.mode === "auto";
                    return (
                      <button
                        key={c.mode}
                        onClick={() => setField("contextMode", c.mode)}
                        title={c.hint || c.label}
                        className={cn(
                          "rounded-md border px-1.5 py-0.5 text-[10.5px] transition-colors",
                          active
                            ? "border-primary/50 bg-primary/10 text-foreground"
                            : "border-border bg-background text-muted-foreground hover:bg-accent"
                        )}
                      >
                        {isAutoChip && <Sparkle className="me-0.5 inline h-2.5 w-2.5" />}
                        {c.label}
                        {active && isAutoChip && pw.contextMode === "auto" && (
                          <span className="ms-1 font-mono text-[9.5px] opacity-70">
                            → {effectiveMode}
                          </span>
                        )}
                      </button>
                    );
                  })}
                  {LEGACY_LABELS[pw.contextMode] && (
                    <span className="rounded-md border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[10.5px] text-amber-600 dark:text-amber-300">
                      {LEGACY_LABELS[pw.contextMode]}
                    </span>
                  )}
                </div>

                {/* Decision label */}
                {contextReason && (
                  <div className="flex items-center gap-1.5 rounded-md border border-dashed border-border bg-muted/30 px-2 py-1 text-[11px] text-muted-foreground">
                    <Sparkle className="h-3 w-3 text-primary" />
                    {contextReason}
                  </div>
                )}

                {/* Large-context warning */}
                {totalChars > LARGE_CONTEXT_THRESHOLD && (
                  <div className="flex items-start gap-1.5 rounded-md border border-amber-500/50 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-700 dark:text-amber-300">
                    <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                    <span>
                      This prompt will use a large context pack
                      {" "}
                      ({(totalChars / 1024).toFixed(1)} KB / ~{estimateTokens(totalChars)} tokens).
                      Consider switching to <b>Selected Only</b> or <b>Recent (3)</b>.
                    </span>
                  </div>
                )}
                {/* Items */}
                <div className="space-y-1">
                  {allItems.map((it) => {
                    const excluded = pw.excludedItemIds.includes(it.id);
                    return (
                      <div
                        key={it.id}
                        className={cn(
                          "rounded-md border bg-card/60 p-2 text-[11.5px]",
                          excluded ? "opacity-40 border-dashed" : "border-border"
                        )}
                      >
                        <div className="flex items-center gap-2">
                          <Badge variant="outline" className="font-mono">
                            {it.kind}
                          </Badge>
                          <span className="truncate font-medium">
                            {it.label.replace(/\s*·\s*trimmed$/i, "")}
                          </span>
                          {/trimmed/i.test(it.label) && (
                            <Badge variant="warning" className="shrink-0 font-mono normal-case">
                              trimmed
                            </Badge>
                          )}
                          <span className="ms-auto font-mono text-[10px] text-muted-foreground">
                            {it.text.length}
                          </span>
                          <button
                            onClick={() => toggleExcluded(it.id)}
                            className="text-muted-foreground hover:text-foreground"
                            title={excluded ? "Include" : "Exclude"}
                          >
                            {excluded ? (
                              <Eye className="h-3 w-3" />
                            ) : (
                              <EyeOff className="h-3 w-3" />
                            )}
                          </button>
                        </div>
                        <div className="mt-1 line-clamp-2 whitespace-pre-wrap break-words font-mono text-[10.5px] text-muted-foreground">
                          {it.text}
                        </div>
                      </div>
                    );
                  })}
                  {allItems.length === 0 && (
                    <div className="rounded-md border border-dashed border-border p-2 text-center text-[11px] text-muted-foreground">
                      No context items. Switch context mode or select text.
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* Rough input */}
          <div className="border-b border-border px-4 py-3">
            <label className="mb-1.5 block text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
              Your idea
            </label>
            {!pw.rough.trim() && (
              <RoughInputExamples
                onPick={(text) => setField("rough", text)}
              />
            )}
            <textarea
              value={pw.rough}
              onChange={(e) => setField("rough", e.target.value)}
              placeholder="Write your idea naturally here. The Prompt Writer will use the context above to help you clarify and structure it."
              dir="auto"
              className={cn(
                "block w-full resize-y rounded-md border border-border bg-background px-3 py-2 text-[13px] outline-none transition-colors placeholder:text-muted-foreground/60 focus:border-primary/60",
                pw.expanded ? "min-h-[260px]" : "min-h-[140px]"
              )}
            />
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <ModeSelector mode={pw.mode} onChange={(m) => setField("mode", m)} />
              <Button
                onClick={onGenerate}
                size="sm"
                className="h-7"
                disabled={busy !== ""}
                title="Generate (Cmd-Enter)"
              >
                {busy === "generate" ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Sparkles className="h-3 w-3" />
                )}
                {draft ? "Regenerate" : "Generate"}
              </Button>
              {pw.rough && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7"
                  onClick={() => setField("rough", "")}
                  title="Clear input"
                >
                  Clear
                </Button>
              )}
              <span className="ms-auto text-[10px] text-muted-foreground">⌘ + Enter</span>
            </div>
          </div>

          {/* Output */}
          {draft && (
            <div className="px-4 py-3">
              <div className="mb-2 flex items-center gap-2">
                <span className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Improved prompt
                </span>
                {draft.model && (
                  <Badge variant="muted" className="font-mono">
                    {draft.model.replace(/^claude-/, "")}
                  </Badge>
                )}
                <span className="ms-auto flex items-center gap-1">
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={onCopy}
                    title="Copy"
                  >
                    {copied ? (
                      <Check className="h-3.5 w-3.5 text-emerald-500" />
                    ) : (
                      <Copy className="h-3.5 w-3.5" />
                    )}
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={() => {
                      if (editing) {
                        setEditing(false);
                      } else {
                        setEditBuf(draft.generated_prompt);
                        setEditing(true);
                      }
                    }}
                    title={editing ? "Cancel edit" : "Edit"}
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={onGenerate}
                    title="Regenerate"
                    disabled={busy !== ""}
                  >
                    <RotateCw
                      className={cn("h-3.5 w-3.5", busy === "generate" && "animate-spin")}
                    />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={onDelete}
                    title="Delete draft"
                  >
                    <Trash2 className="h-3.5 w-3.5 text-destructive/70" />
                  </Button>
                </span>
              </div>
              {editing ? (
                <>
                  <textarea
                    value={editBuf}
                    onChange={(e) => setEditBuf(e.target.value)}
                    dir="auto"
                    className="block w-full min-h-[260px] resize-y rounded-md border border-border bg-background px-3 py-2 font-mono text-[12px] outline-none focus:border-primary/60"
                  />
                  <div className="mt-2 flex gap-2">
                    <Button size="sm" className="h-7" onClick={onSaveEdit}>
                      <Save className="h-3 w-3" /> Save
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-7"
                      onClick={() => setEditing(false)}
                    >
                      Cancel
                    </Button>
                  </div>
                </>
              ) : (
                <div
                  dir="auto"
                  className="rounded-md border border-border bg-card/60 p-3"
                >
                  <pre className="whitespace-pre-wrap break-words font-mono text-[12px] leading-relaxed">
                    {draft.generated_prompt}
                  </pre>
                </div>
              )}

              {draft.improvement_notes && (
                <div className="mt-3">
                  <div className="mb-1 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                    What I improved
                  </div>
                  <div dir="auto" className="prose-msg text-[12px]">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {draft.improvement_notes}
                    </ReactMarkdown>
                  </div>
                </div>
              )}

              {/* Refine */}
              <div className="mt-3 flex gap-2">
                <input
                  value={refineText}
                  onChange={(e) => setRefineText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && refineText.trim() && busy === "") onRefine();
                  }}
                  placeholder='Refine: "make it shorter", "add testing requirements"…'
                  className="min-w-0 flex-1 rounded-md border border-border bg-background px-3 py-1.5 text-[12px] outline-none focus:border-primary/60"
                />
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7"
                  onClick={onRefine}
                  disabled={!refineText.trim() || busy !== ""}
                >
                  {busy === "refine" ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <Wand2 className="h-3 w-3" />
                  )}
                  Refine
                </Button>
              </div>
            </div>
          )}

          {!draft && (
            <div className="px-4 py-8 text-center text-xs text-muted-foreground">
              Choose a mode, write your idea, click Generate. Use{" "}
              <kbd className="rounded border bg-muted px-1 font-mono">⌘ Enter</kbd> as a shortcut.
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function ModeSelector({
  mode,
  onChange,
}: {
  mode: PromptWriterMode;
  onChange: (m: PromptWriterMode) => void;
}) {
  const [open, setOpen] = React.useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" className="h-7">
          <span className="text-muted-foreground">Mode:</span>
          <span>{MODE_LABELS[mode].label}</span>
          <ChevronDown className="h-3 w-3 opacity-60" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-72 p-1">
        {(Object.keys(MODE_LABELS) as PromptWriterMode[]).map((m) => (
          <button
            key={m}
            onClick={() => {
              onChange(m);
              setOpen(false);
            }}
            className={cn(
              "flex w-full items-start gap-2 rounded px-2 py-1.5 text-left hover:bg-accent",
              mode === m && "bg-accent/60"
            )}
          >
            <div className="min-w-0 flex-1">
              <div className="text-[12.5px] font-medium">{MODE_LABELS[m].label}</div>
              <div className="text-[11px] text-muted-foreground">{MODE_LABELS[m].hint}</div>
            </div>
            {mode === m && <X className="hidden" />}
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
}

// Starter prompts shown above the textarea when it's empty. Click to fill the
// textarea with the example text — the user then edits it before generating.
const ROUGH_EXAMPLES: { label: string; text: string }[] = [
  {
    label: "Turn this into a bug report",
    text: "Turn this into a clean bug report with Observed / Expected / Repro / Fix / Verify sections. Use the selected turn(s) as the source of truth.",
  },
  {
    label: "Make this prompt clearer",
    text: "Take my rough draft below and rewrite it as a clearer, more specific prompt. Keep the original intent; fix grammar and structure.\n\nDraft:\n",
  },
  {
    label: "Continue from the previous idea",
    text: "Continue the conversation from where it left off. Pick up the previous idea and propose the next concrete step.",
  },
  {
    label: "Write a Claude Code task prompt",
    text: "Write a structured Claude Code task prompt for this work. Include scope, files to touch, constraints, and an explicit done-when checklist.",
  },
];

function RoughInputExamples({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="mb-2 rounded-md border border-dashed border-border/70 bg-muted/30 p-2">
      <div className="mb-1 text-[10.5px] uppercase tracking-wider text-muted-foreground">
        Try a starter
      </div>
      <div className="flex flex-wrap gap-1.5">
        {ROUGH_EXAMPLES.map((ex) => (
          <button
            key={ex.label}
            type="button"
            onClick={() => onPick(ex.text)}
            className="rounded-full border border-border bg-background px-2.5 py-0.5 text-[11.5px] text-muted-foreground transition-colors hover:border-primary/50 hover:bg-accent hover:text-foreground"
            title={ex.text}
          >
            {ex.label}
          </button>
        ))}
      </div>
    </div>
  );
}
