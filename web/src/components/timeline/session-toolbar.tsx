import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Languages,
  Loader2,
  Download,
  FileText,
  MessageSquareQuote,
  X,
  ChevronDown,
  Search,
  User as UserIcon,
  Bot,
  Wrench,
  Sparkles,
  Coins,
  Filter as FilterIcon,
  Wand2,
  ClipboardCheck,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useApp } from "@/store/app";
import { api, type TranscriptEvent } from "@/lib/api";
import { toast } from "sonner";
import { cn, formatTime, shortPath } from "@/lib/utils";

export function SessionToolbar() {
  const session = useApp((s) => s.session);
  const translations = useApp((s) => s.translations);
  const setTranslation = useApp((s) => s.setTranslation);
  const mergeTranslations = useApp((s) => s.mergeTranslations);
  const setShownTranslated = useApp((s) => s.setShownTranslated);
  const shownTranslated = useApp((s) => s.shownTranslated);
  const filterQuery = useApp((s) => s.filterQuery);
  const setFilterQuery = useApp((s) => s.setFilterQuery);
  const filterRoles = useApp((s) => s.filterRoles);
  const setFilterRoles = useApp((s) => s.setFilterRoles);
  const filterTool = useApp((s) => s.filterTool);
  const setFilterTool = useApp((s) => s.setFilterTool);
  const setSummary = useApp((s) => s.setSummary);
  const summary = useApp((s) => s.summary);
  const summaryOpen = useApp((s) => s.summaryOpen);
  const setSummaryOpen = useApp((s) => s.setSummaryOpen);
  const openPromptWriter = useApp((s) => s.openPromptWriter);
  const openReviewPanel = useApp((s) => s.openReviewPanel);

  const [translateAll, setTranslateAll] = React.useState<{
    running: boolean;
    done: number;
    total: number;
    cancelled: boolean;
  } | null>(null);
  const cancelRef = React.useRef(false);
  const [searchOpen, setSearchOpen] = React.useState(false);
  const searchRef = React.useRef<HTMLInputElement>(null);
  const [busySummary, setBusySummary] = React.useState(false);

  // Reset filters when switching sessions
  React.useEffect(() => {
    setFilterQuery("");
    setFilterTool(null);
    setFilterRoles({ user: true, assistant: true });
    setSearchOpen(false);
  }, [session?.meta.session_id, setFilterQuery, setFilterTool, setFilterRoles]);

  // Cmd-F / Ctrl-F: open the search input
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") {
        e.preventDefault();
        setSearchOpen(true);
        setTimeout(() => searchRef.current?.focus(), 50);
      }
      if (e.key === "Escape" && searchOpen) {
        setSearchOpen(false);
        setFilterQuery("");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [searchOpen, setFilterQuery]);

  if (!session) return null;

  const turns = session.events.filter(
    (e) =>
      !e.is_command_artifact &&
      ((e.role === "user" && e.user_text) ||
        (e.role === "assistant" && e.text_blocks.length > 0))
  );
  const turnCount = turns.length;
  const translatedCount = turns.filter(
    (e) => translations[e.uuid] && translations[e.uuid] !== "pending"
  ).length;
  const allShown = turns.length > 0 && turns.every((e) => shownTranslated[e.uuid]);

  // Tool usage map across the whole session (for filter chips)
  const toolCounts: Record<string, number> = {};
  for (const ev of session.events) {
    for (const tu of ev.tool_uses) {
      toolCounts[tu.name] = (toolCounts[tu.name] ?? 0) + 1;
    }
  }
  const topTools = Object.entries(toolCounts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6);

  const onTranslateAll = async () => {
    if (translateAll?.running) {
      cancelRef.current = true;
      return;
    }
    cancelRef.current = false;
    if (allShown) {
      turns.forEach((e) => setShownTranslated(e.uuid, false));
      toast.success("Showing originals");
      return;
    }
    const need: { key: string; text: string }[] = [];
    for (const e of turns) {
      const existing = translations[e.uuid];
      if (existing && existing !== "pending") continue;
      const text = e.role === "assistant" ? e.text_blocks.join("\n\n") : e.user_text || "";
      if (text.trim()) need.push({ key: e.uuid, text });
    }
    if (need.length) {
      try {
        const r = await api.translateLookupBatch(need, "ar");
        if (Object.keys(r.hits).length) mergeTranslations(r.hits);
      } catch {}
    }
    const stillNeed = turns.filter((e) => {
      const v = useApp.getState().translations[e.uuid];
      return !v || v === "pending";
    });
    if (stillNeed.length === 0) {
      turns.forEach((e) => setShownTranslated(e.uuid, true));
      toast.success(`All ${turns.length} turns shown in Arabic (cached)`);
      return;
    }
    setTranslateAll({ running: true, done: 0, total: stillNeed.length, cancelled: false });
    let done = 0;
    let failed = 0;
    for (const e of stillNeed) {
      if (cancelRef.current) break;
      const text = e.role === "assistant" ? e.text_blocks.join("\n\n") : e.user_text || "";
      setTranslation(e.uuid, "pending");
      try {
        const r = await api.translate(text, "ar");
        setTranslation(e.uuid, { translation: r.translation, model: r.model });
      } catch {
        setTranslation(e.uuid, null);
        failed++;
      }
      done++;
      setTranslateAll((s) => (s ? { ...s, done } : s));
    }
    turns.forEach((e) => {
      const v = useApp.getState().translations[e.uuid];
      if (v && v !== "pending") setShownTranslated(e.uuid, true);
    });
    setTranslateAll(null);
    if (cancelRef.current) {
      toast.info(`Stopped at ${done}/${stillNeed.length}`);
    } else if (failed > 0) {
      toast.warning(`Translated ${done - failed}/${stillNeed.length}, ${failed} failed`);
    } else {
      toast.success(`Translated ${done} turn${done === 1 ? "" : "s"}`);
    }
  };

  const onSummarize = async (force = false) => {
    if (busySummary) return;
    setBusySummary(true);
    try {
      const transcript = buildCompactTranscript(turns);
      const r = await api.summarizeSession({
        session_id: session.meta.session_id,
        transcript,
        force,
      });
      setSummary({
        sessionId: session.meta.session_id,
        text: r.summary,
        model: r.model,
        cached: r.cached,
      });
      setSummaryOpen(true);
      toast.success(r.cached ? "Summary loaded (cached)" : "Summary generated");
    } catch (e: any) {
      toast.error(e?.message ?? "Summarize failed");
    } finally {
      setBusySummary(false);
    }
  };

  const filtersActive =
    !!filterQuery || !filterRoles.user || !filterRoles.assistant || !!filterTool;

  return (
    <div className="sticky top-0 z-10 flex flex-col gap-1.5 border-b border-border bg-background/95 px-3 py-1.5 backdrop-blur-sm">
      {/* Row 1: counts, cwd, primary actions */}
      <div className="flex min-w-0 items-center gap-2">
        <StatsPopover turns={turns} session={session} />
        {translatedCount > 0 && (
          <Badge variant="info" className="font-mono">
            {translatedCount} translated
          </Badge>
        )}
        {session.meta.cwd && (
          <span className="ms-1 truncate font-mono text-[11px] text-muted-foreground">
            {shortPath(session.meta.cwd, 50)}
          </span>
        )}
        <div className="ms-auto flex items-center gap-1">
          <Button
            variant={searchOpen ? "secondary" : "ghost"}
            size="icon"
            className="h-7 w-7"
            title="Search (Cmd-F)"
            onClick={() => {
              setSearchOpen((o) => !o);
              if (!searchOpen) setTimeout(() => searchRef.current?.focus(), 50);
            }}
          >
            <Search className="h-3 w-3" />
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-7"
            title="AI summary of this session"
            onClick={() => onSummarize()}
            disabled={busySummary}
          >
            {busySummary ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <Sparkles className="h-3 w-3" />
            )}
            Summarize
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-7"
            title="Context-Aware Prompt Writer"
            onClick={() =>
              openPromptWriter({
                sourceEventUuid: null,
                selectedText: null,
              })
            }
          >
            <Wand2 className="h-3 w-3" />
            Write prompt
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-7"
            title="Discuss this work with another reviewer (Codex)"
            onClick={() =>
              openReviewPanel({
                sourceTurnUuid: null,
                sourceTurnRole: null,
                sourceTurnText: null,
              })
            }
          >
            <ClipboardCheck className="h-3 w-3" />
            Review
          </Button>
          {translateAll?.running && (
            <span className="flex items-center gap-1.5 rounded bg-muted/60 px-2 py-0.5 text-[11px] tabular-nums">
              <Loader2 className="h-3 w-3 animate-spin" />
              {translateAll.done}/{translateAll.total}
            </span>
          )}
          <Button
            variant={allShown && !translateAll?.running ? "secondary" : "outline"}
            size="sm"
            onClick={onTranslateAll}
            className="h-7"
            title={
              translateAll?.running
                ? "Click to stop"
                : allShown
                ? "Show all originals"
                : "Translate every turn to Arabic"
            }
          >
            {translateAll?.running ? (
              <>
                <X className="h-3 w-3" />
                Stop
              </>
            ) : (
              <>
                <Languages className="h-3 w-3" />
                {allShown ? "Hide all" : "Translate all"}
              </>
            )}
          </Button>
          <ExportMenu session={session} />
        </div>
      </div>

      {/* Row 2: search + filters (only when toggled or active) */}
      {(searchOpen || filtersActive) && (
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex flex-1 items-center gap-2 rounded-md border border-border bg-background px-2 py-0.5">
            <Search className="h-3 w-3 shrink-0 text-muted-foreground" />
            <input
              ref={searchRef}
              value={filterQuery}
              onChange={(e) => setFilterQuery(e.target.value)}
              placeholder="Search within session…"
              className="min-w-0 flex-1 bg-transparent py-1 text-[12px] outline-none"
            />
            {filterQuery && (
              <button
                onClick={() => setFilterQuery("")}
                className="text-muted-foreground hover:text-foreground"
                title="Clear"
              >
                <X className="h-3 w-3" />
              </button>
            )}
          </div>
          <RoleChip
            icon={<UserIcon className="h-3 w-3" />}
            label="You"
            active={filterRoles.user}
            onClick={() => setFilterRoles({ ...filterRoles, user: !filterRoles.user })}
          />
          <RoleChip
            icon={<Bot className="h-3 w-3" />}
            label="Claude"
            active={filterRoles.assistant}
            onClick={() =>
              setFilterRoles({ ...filterRoles, assistant: !filterRoles.assistant })
            }
          />
          <Popover>
            <PopoverTrigger asChild>
              <button
                className={cn(
                  "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] transition-colors",
                  filterTool
                    ? "border-amber-500/50 bg-amber-500/10 text-amber-600 dark:text-amber-300"
                    : "border-border bg-background text-muted-foreground hover:bg-accent"
                )}
              >
                <Wrench className="h-3 w-3" />
                {filterTool || "Any tool"}
                <ChevronDown className="h-3 w-3 opacity-60" />
              </button>
            </PopoverTrigger>
            <PopoverContent className="w-48 p-1">
              <button
                onClick={() => setFilterTool(null)}
                className="flex w-full items-center justify-between rounded px-2 py-1 text-left text-[12px] hover:bg-accent"
              >
                <span>Any tool</span>
                {!filterTool && <span className="text-primary">•</span>}
              </button>
              <div className="my-0.5 h-px bg-border" />
              {topTools.length === 0 && (
                <div className="px-2 py-1 text-[11px] text-muted-foreground">No tools</div>
              )}
              {topTools.map(([name, count]) => (
                <button
                  key={name}
                  onClick={() => setFilterTool(name === filterTool ? null : name)}
                  className="flex w-full items-center justify-between rounded px-2 py-1 text-left text-[12px] hover:bg-accent"
                >
                  <span className="font-mono">{name}</span>
                  <span className="text-muted-foreground">
                    {count}
                    {name === filterTool && " •"}
                  </span>
                </button>
              ))}
            </PopoverContent>
          </Popover>
          {filtersActive && (
            <button
              onClick={() => {
                setFilterQuery("");
                setFilterTool(null);
                setFilterRoles({ user: true, assistant: true });
              }}
              className="text-[11px] text-muted-foreground hover:text-foreground"
              title="Clear all filters"
            >
              clear
            </button>
          )}
        </div>
      )}

      {/* Inline summary panel when generated */}
      {summaryOpen && summary && summary.sessionId === session.meta.session_id && (
        <div className="rounded-md border border-border bg-card p-3">
          <div className="mb-2 flex items-center gap-2 text-[11px] text-muted-foreground">
            <Sparkles className="h-3 w-3" />
            <span className="font-medium text-foreground">Session summary</span>
            <span className="font-mono">{summary.model.replace(/^claude-/, "")}</span>
            {summary.cached && <Badge variant="muted">cached</Badge>}
            <button
              onClick={() => onSummarize(true)}
              className="ms-auto text-muted-foreground hover:text-foreground"
              title="Regenerate"
              disabled={busySummary}
            >
              <Loader2
                className={cn("h-3 w-3", busySummary && "animate-spin")}
              />
            </button>
            <button
              onClick={() => setSummaryOpen(false)}
              className="text-muted-foreground hover:text-foreground"
              title="Close"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
          <div dir="auto" className="prose-msg text-[12.5px]">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{summary.text}</ReactMarkdown>
          </div>
        </div>
      )}
    </div>
  );
}

function RoleChip({
  icon,
  label,
  active,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] transition-colors",
        active
          ? "border-primary/50 bg-primary/10 text-foreground"
          : "border-border bg-background text-muted-foreground line-through opacity-60 hover:opacity-100"
      )}
      title={active ? `Hide ${label}` : `Show ${label}`}
    >
      {icon}
      {label}
    </button>
  );
}

function StatsPopover({
  turns,
  session,
}: {
  turns: TranscriptEvent[];
  session: NonNullable<ReturnType<typeof useApp.getState>["session"]>;
}) {
  // Aggregate token usage
  let inSum = 0,
    outSum = 0,
    cacheReadSum = 0,
    cacheCreateSum = 0;
  const modelCounts: Record<string, { calls: number; out: number }> = {};
  for (const ev of session.events) {
    if (!ev.usage) continue;
    inSum += ev.usage.input_tokens;
    outSum += ev.usage.output_tokens;
    cacheReadSum += ev.usage.cache_read_input_tokens;
    cacheCreateSum += ev.usage.cache_creation_input_tokens;
    if (ev.model) {
      modelCounts[ev.model] = modelCounts[ev.model] ?? { calls: 0, out: 0 };
      modelCounts[ev.model].calls++;
      modelCounts[ev.model].out += ev.usage.output_tokens;
    }
  }
  const totalIn = inSum + cacheReadSum + cacheCreateSum;
  const cacheHitPct = totalIn > 0 ? (cacheReadSum / totalIn) * 100 : 0;
  const costEst = estimateCost({ inSum, outSum, cacheReadSum, cacheCreateSum, modelCounts });
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          className="inline-flex items-center gap-1 rounded-md border border-transparent bg-muted px-1.5 py-0.5 text-[10.5px] font-medium text-muted-foreground hover:border-border"
          title="Session stats"
        >
          <Coins className="h-2.5 w-2.5" />
          {turns.length} turn{turns.length === 1 ? "" : "s"}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-72 p-3">
        <div className="space-y-2 text-[12px]">
          <div className="flex items-center justify-between text-[11px] uppercase tracking-wider text-muted-foreground">
            <span>Tokens</span>
            <span>{turns.length} turns</span>
          </div>
          <StatRow label="Output" value={fmt(outSum)} />
          <StatRow label="Input (fresh)" value={fmt(inSum)} />
          <StatRow label="Cache write" value={fmt(cacheCreateSum)} />
          <StatRow
            label="Cache read"
            value={`${fmt(cacheReadSum)} (${cacheHitPct.toFixed(0)}%)`}
            highlight={cacheHitPct >= 50}
          />
          <div className="my-1 h-px bg-border" />
          <StatRow
            label="Est. cost"
            value={costEst > 0 ? `$${costEst.toFixed(3)}` : "—"}
            highlight
          />
          <div className="my-1 h-px bg-border" />
          <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
            Models
          </div>
          {Object.entries(modelCounts).length === 0 && (
            <div className="text-[11px] text-muted-foreground">No usage data</div>
          )}
          {Object.entries(modelCounts)
            .sort((a, b) => b[1].calls - a[1].calls)
            .map(([m, v]) => (
              <div key={m} className="flex items-center justify-between">
                <span className="truncate font-mono text-[11px]">{m}</span>
                <span className="text-[11px] text-muted-foreground">
                  {v.calls} calls · {fmt(v.out)} out
                </span>
              </div>
            ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}

function StatRow({
  label,
  value,
  highlight,
}: {
  label: string;
  value: string;
  highlight?: boolean;
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-muted-foreground">{label}</span>
      <span
        className={cn(
          "font-mono tabular-nums",
          highlight ? "text-emerald-600 dark:text-emerald-400" : "text-foreground"
        )}
      >
        {value}
      </span>
    </div>
  );
}

function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return n.toLocaleString();
}

// Rough $/MTok pricing for common models. Cache read is typically 0.1× input.
const PRICING: Record<string, { in: number; out: number; cacheRead: number; cacheWrite: number }> = {
  "claude-haiku-4-5-20251001": { in: 1, out: 5, cacheRead: 0.1, cacheWrite: 1.25 },
  "claude-sonnet-4-6": { in: 3, out: 15, cacheRead: 0.3, cacheWrite: 3.75 },
  "claude-opus-4-7": { in: 15, out: 75, cacheRead: 1.5, cacheWrite: 18.75 },
};

function estimateCost(o: {
  inSum: number;
  outSum: number;
  cacheReadSum: number;
  cacheCreateSum: number;
  modelCounts: Record<string, { calls: number; out: number }>;
}): number {
  // Use the model with the most output tokens as the dominant pricing tier.
  let best: { model: string; out: number } | null = null;
  for (const [m, v] of Object.entries(o.modelCounts)) {
    if (!best || v.out > best.out) best = { model: m, out: v.out };
  }
  const p = PRICING[best?.model ?? ""] ?? PRICING["claude-sonnet-4-6"];
  return (
    (o.inSum * p.in +
      o.outSum * p.out +
      o.cacheReadSum * p.cacheRead +
      o.cacheCreateSum * p.cacheWrite) /
    1_000_000
  );
}

function ExportMenu({
  session,
}: {
  session: NonNullable<ReturnType<typeof useApp.getState>["session"]>;
}) {
  const [open, setOpen] = React.useState(false);
  const turns = session.events.filter(
    (e) =>
      !e.is_command_artifact &&
      ((e.role === "user" && e.user_text) ||
        (e.role === "assistant" && (e.text_blocks.length > 0 || e.tool_uses.length > 0)))
  );

  const downloadFile = (filename: string, content: string, mime = "text/markdown") => {
    const blob = new Blob([content], { type: `${mime};charset=utf-8` });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const baseName = `session-${session.meta.session_id.slice(0, 8)}`;
  const cwdLabel = session.meta.cwd?.split(/[\\/]/).filter(Boolean).pop() ?? "";

  const exportFullMd = () => {
    const md = renderSessionMarkdown(session.meta, turns);
    downloadFile(`${cwdLabel || baseName}-full.md`, md);
    toast.success("Exported full transcript");
    setOpen(false);
  };
  const exportPromptsMd = () => {
    const md = renderPromptsMarkdown(session.meta, turns);
    downloadFile(`${cwdLabel || baseName}-prompts.md`, md);
    toast.success("Exported prompts");
    setOpen(false);
  };
  const copyPrompts = async () => {
    const md = renderPromptsMarkdown(session.meta, turns);
    try {
      await navigator.clipboard.writeText(md);
      toast.success("Prompts copied");
    } catch {
      toast.error("Clipboard write failed");
    }
    setOpen(false);
  };
  const copyFull = async () => {
    const md = renderSessionMarkdown(session.meta, turns);
    try {
      await navigator.clipboard.writeText(md);
      toast.success("Transcript copied");
    } catch {
      toast.error("Clipboard write failed");
    }
    setOpen(false);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" className="h-7">
          <Download className="h-3 w-3" />
          Export
          <ChevronDown className="h-3 w-3 opacity-60" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-64 p-1">
        <ExportItem
          icon={<FileText className="h-3.5 w-3.5" />}
          label="Full transcript (.md)"
          hint={`${turns.length} turns, formatted markdown`}
          onClick={exportFullMd}
        />
        <ExportItem
          icon={<MessageSquareQuote className="h-3.5 w-3.5" />}
          label="My prompts only (.md)"
          hint="Numbered list of your inputs"
          onClick={exportPromptsMd}
        />
        <div className="my-0.5 h-px bg-border" />
        <ExportItem
          icon={<FileText className="h-3.5 w-3.5" />}
          label="Copy full transcript"
          hint="Markdown to clipboard"
          onClick={copyFull}
        />
        <ExportItem
          icon={<MessageSquareQuote className="h-3.5 w-3.5" />}
          label="Copy prompts"
          hint="Markdown to clipboard"
          onClick={copyPrompts}
        />
      </PopoverContent>
    </Popover>
  );
}

function ExportItem({
  icon,
  label,
  hint,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  hint: string;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="group flex w-full items-start gap-2 rounded px-2 py-1.5 text-left hover:bg-accent"
    >
      <div className="mt-0.5 text-muted-foreground group-hover:text-foreground">{icon}</div>
      <div className="min-w-0 flex-1">
        <div className="text-[12.5px] font-medium">{label}</div>
        <div className="text-[11px] text-muted-foreground">{hint}</div>
      </div>
    </button>
  );
}

// ---- markdown serializers ----

function renderSessionMarkdown(
  meta: NonNullable<ReturnType<typeof useApp.getState>["session"]>["meta"],
  turns: TranscriptEvent[]
): string {
  const lines: string[] = [];
  lines.push(`# Claude Code Session — ${meta.ai_title || meta.session_id.slice(0, 8)}`);
  lines.push("");
  lines.push(`- **Session ID:** \`${meta.session_id}\``);
  if (meta.cwd) lines.push(`- **Working dir:** \`${meta.cwd}\``);
  if (meta.git_branch) lines.push(`- **Git branch:** \`${meta.git_branch}\``);
  if (meta.entrypoint) lines.push(`- **Entrypoint:** ${meta.entrypoint}`);
  if (meta.version) lines.push(`- **Claude Code version:** ${meta.version}`);
  lines.push(`- **Bucket:** \`${meta.bucket}\``);
  lines.push("");
  lines.push("---");
  lines.push("");
  for (const ev of turns) {
    const ts = ev.timestamp ? formatTime(ev.timestamp) : "";
    if (ev.role === "user") {
      lines.push(`## You${ts ? ` — ${ts}` : ""}`);
      lines.push("");
      if (ev.user_text) lines.push(ev.user_text);
      lines.push("");
    } else if (ev.role === "assistant") {
      const modelTag = ev.model ? ` (\`${ev.model}\`)` : "";
      lines.push(`## Claude${modelTag}${ts ? ` — ${ts}` : ""}`);
      if (ev.attribution_skill) lines.push(`> _via skill_ \`${ev.attribution_skill}\``);
      lines.push("");
      if (ev.text_blocks.length) {
        lines.push(ev.text_blocks.join("\n\n"));
        lines.push("");
      }
      for (const tu of ev.tool_uses) {
        const summary = oneLine(tu);
        lines.push(`> **🔧 ${tu.name}** — ${summary}`);
        lines.push("");
      }
    }
  }
  lines.push("");
  lines.push(`_Exported via Claude Watcher — ${new Date().toISOString()}_`);
  return lines.join("\n");
}

function renderPromptsMarkdown(
  meta: NonNullable<ReturnType<typeof useApp.getState>["session"]>["meta"],
  turns: TranscriptEvent[]
): string {
  const prompts = turns.filter(
    (e) => e.role === "user" && !!e.user_text && !e.is_command_artifact
  );
  const lines: string[] = [];
  lines.push(`# Prompts — ${meta.ai_title || meta.session_id.slice(0, 8)}`);
  if (meta.cwd) lines.push(`_${meta.cwd}_`);
  lines.push("");
  lines.push(`Total: ${prompts.length}`);
  lines.push("");
  prompts.forEach((p, i) => {
    const ts = p.timestamp ? formatTime(p.timestamp) : "";
    lines.push(`### ${i + 1}.${ts ? ` ${ts}` : ""}`);
    lines.push("");
    lines.push(p.user_text!.trim());
    lines.push("");
  });
  return lines.join("\n");
}

function oneLine(tu: TranscriptEvent["tool_uses"][number]): string {
  const i = tu.input ?? {};
  if (tu.name === "Bash" || tu.name === "PowerShell") {
    return "`" + (String(i.command ?? "").split("\n")[0].slice(0, 200)) + "`";
  }
  if (tu.name === "Read" || tu.name === "Write" || tu.name === "Edit" || tu.name === "MultiEdit") {
    return `\`${i.file_path ?? ""}\``;
  }
  if (tu.name === "Grep" || tu.name === "Glob") return "`" + (i.pattern ?? i.glob ?? "") + "`";
  if (tu.name === "WebFetch") return `\`${i.url ?? ""}\``;
  if (tu.name === "WebSearch") return `\`${i.query ?? ""}\``;
  try {
    return "`" + JSON.stringify(i).slice(0, 200) + "`";
  } catch {
    return "";
  }
}

// Compact transcript for the AI summary endpoint
function buildCompactTranscript(turns: TranscriptEvent[]): string {
  const lines: string[] = [];
  for (const ev of turns) {
    if (ev.role === "user" && ev.user_text) {
      lines.push(`USER: ${ev.user_text.slice(0, 4000)}`);
    } else if (ev.role === "assistant") {
      const txt = ev.text_blocks.join("\n").slice(0, 4000);
      if (txt) lines.push(`CLAUDE: ${txt}`);
      const tools = ev.tool_uses.map((t) => `${t.name}(${oneLine(t).slice(0, 80)})`);
      if (tools.length) lines.push(`TOOLS: ${tools.join("; ")}`);
    }
    lines.push("");
  }
  return lines.join("\n").slice(0, 200_000); // hard cap
}
