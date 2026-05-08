import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Languages,
  Loader2,
  User,
  Bot,
  Copy,
  ExternalLink,
  Wand2,
  ClipboardCheck,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from "@/components/ui/tooltip";
import { ToolCard } from "./tool-card";
import { useApp } from "@/store/app";
import { api, type TranscriptEvent, type ToolResult } from "@/lib/api";
import { cn, formatTime } from "@/lib/utils";
import { toast } from "sonner";

interface Props {
  event: TranscriptEvent;
  toolResultsById: Record<string, ToolResult>;
}

export function Turn({ event, toolResultsById }: Props) {
  const isUser = event.role === "user";
  const isAssistant = event.role === "assistant";
  const text = isUser ? event.user_text ?? "" : event.text_blocks.join("\n\n");
  const hasContent = !!text || event.tool_uses.length > 0;
  if (!hasContent) return null;

  return (
    <article
      data-turn-id={event.uuid}
      className={cn(
        "group/turn flex gap-3 px-4 py-3",
        isUser && "bg-muted/30"
      )}
    >
      <Avatar role={event.role} />
      <div className="min-w-0 flex-1">
        <TurnHeader event={event} />
        {text && <TurnBody text={text} uuid={event.uuid} role={event.role} />}
        {isAssistant && event.tool_uses.length > 0 && (
          <div className="mt-1.5">
            {event.tool_uses.map((u) => (
              <ToolCard key={u.id} use={u} result={toolResultsById[u.id]} />
            ))}
          </div>
        )}
        {event.pr?.url && (
          <a
            href={event.pr.url}
            target="_blank"
            rel="noreferrer"
            className="mt-2 inline-flex items-center gap-1 rounded-md border border-sky-500/30 bg-sky-500/10 px-2 py-1 text-[12px] text-sky-700 dark:text-sky-300"
          >
            <ExternalLink className="h-3 w-3" /> PR #{event.pr.number} · {event.pr.repository}
          </a>
        )}
      </div>
    </article>
  );
}

function Avatar({ role }: { role: string | null }) {
  if (role === "user") {
    return (
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-blue-500/15 text-blue-600 dark:text-blue-300">
        <User className="h-3.5 w-3.5" />
      </div>
    );
  }
  return (
    <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-violet-500/15 text-violet-600 dark:text-violet-300">
      <Bot className="h-3.5 w-3.5" />
    </div>
  );
}

function TurnHeader({ event }: { event: TranscriptEvent }) {
  const role = event.role === "user" ? "You" : "Claude";
  return (
    <div className="mb-1 flex items-center gap-2 text-[11px]">
      <span className="font-medium">{role}</span>
      {event.model && (
        <Badge variant="muted" className="font-mono">
          {event.model.replace(/^claude-/, "")}
        </Badge>
      )}
      {event.attribution_skill && (
        <Badge variant="info" className="font-mono">via {event.attribution_skill}</Badge>
      )}
      {event.is_sidechain && <Badge variant="warning">subagent</Badge>}
      <span className="text-muted-foreground">{formatTime(event.timestamp)}</span>
    </div>
  );
}

function TurnBody({ text, uuid, role }: { text: string; uuid: string; role: string | null }) {
  const translation = useApp((s) => s.translations[uuid]);
  const setTranslation = useApp((s) => s.setTranslation);
  const showTr = useApp((s) => s.shownTranslated[uuid] ?? false);
  const setShowTr = useApp((s) => s.setShownTranslated);
  const openPromptWriter = useApp((s) => s.openPromptWriter);
  const openReviewPanel = useApp((s) => s.openReviewPanel);

  const onToggle = async () => {
    if (showTr) {
      setShowTr(uuid, false);
      return;
    }
    if (translation && translation !== "pending") {
      setShowTr(uuid, true);
      return;
    }
    setTranslation(uuid, "pending");
    try {
      const r = await api.translate(text, "ar");
      setTranslation(uuid, { translation: r.translation, model: r.model });
      setShowTr(uuid, true);
      if (!r.cached) toast.success("Translated");
    } catch (e: any) {
      setTranslation(uuid, null);
      toast.error(e?.message ?? "Translation failed");
    }
  };

  const translated = translation && translation !== "pending" ? translation.translation : null;
  const pending = translation === "pending";

  return (
    <div className="relative">
      <div className="absolute -end-2 top-0 flex flex-col gap-1 opacity-0 transition-opacity group-hover/turn:opacity-100">
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant={showTr ? "secondary" : "ghost"}
                size="icon"
                onClick={onToggle}
                disabled={pending}
                className={cn("h-6 w-6", pending && "opacity-100")}
              >
                {pending ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Languages className="h-3 w-3" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent>{showTr ? "Show original" : "Translate to Arabic"}</TooltipContent>
          </Tooltip>
        </TooltipProvider>
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                onClick={() =>
                  openPromptWriter({
                    sourceEventUuid: uuid,
                    selectedText: null,
                    contextMode: "selected_plus_nearby",
                  })
                }
                className="h-6 w-6"
              >
                <Wand2 className="h-3 w-3" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Write better prompt from this turn</TooltipContent>
          </Tooltip>
        </TooltipProvider>
        {role === "assistant" && (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() =>
                    openReviewPanel({
                      sourceTurnUuid: uuid,
                      sourceTurnRole: role,
                      sourceTurnText: text,
                    })
                  }
                  className="h-6 w-6"
                >
                  <ClipboardCheck className="h-3 w-3" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Send this turn to a reviewer</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
      </div>
      {showTr && translated ? (
        <div dir="rtl" className="prose-msg font-arabic">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{translated}</ReactMarkdown>
        </div>
      ) : role === "user" ? (
        <div dir="auto" className="prose-msg whitespace-pre-wrap break-words">{text}</div>
      ) : (
        <div dir="auto" className="prose-msg">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
        </div>
      )}
      {role === "assistant" && (
        <AssistantActionRow uuid={uuid} role={role} text={text} />
      )}
    </div>
  );
}

/** Always-visible action row anchored to an assistant message. The Review
 *  button captures THIS exact message as the review subject — the panel's
 *  source_turn_uuid is set to ``uuid`` and the subject card shows
 *  ``text``. No reviewer call is fired by clicking Review; the user must
 *  press Send (or enable Auto Review and Send) inside the panel. */
function AssistantActionRow({
  uuid,
  role,
  text,
}: {
  uuid: string;
  role: string | null;
  text: string;
}) {
  const openPromptWriter = useApp((s) => s.openPromptWriter);
  const openReviewPanel = useApp((s) => s.openReviewPanel);

  const onReview = () => {
    openReviewPanel({
      sourceTurnUuid: uuid,
      sourceTurnRole: role,
      sourceTurnText: text,
    });
  };

  const onWritePrompt = () => {
    openPromptWriter({
      sourceEventUuid: uuid,
      selectedText: null,
      contextMode: "selected_plus_nearby",
    });
  };

  const onCopy = async () => {
    if (!text || !text.trim()) return;
    try {
      await navigator.clipboard.writeText(text);
      toast.success("Copied message");
    } catch {
      toast.error("Clipboard write failed");
    }
  };

  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
      <button
        type="button"
        onClick={onReview}
        className="inline-flex items-center gap-1 rounded-md border border-border bg-background/40 px-2 py-0.5 transition-colors hover:border-primary/50 hover:bg-primary/5 hover:text-foreground"
        title="Open Review Chat anchored to this exact Claude message"
      >
        <ClipboardCheck className="h-3 w-3" />
        Review this
      </button>
      <button
        type="button"
        onClick={onWritePrompt}
        className="inline-flex items-center gap-1 rounded-md border border-border bg-background/40 px-2 py-0.5 transition-colors hover:bg-accent/40 hover:text-foreground"
        title="Open the Prompt Writer with this turn as the source"
      >
        <Wand2 className="h-3 w-3" />
        Write prompt from this
      </button>
      <button
        type="button"
        onClick={onCopy}
        className="inline-flex items-center gap-1 rounded-md border border-border bg-background/40 px-2 py-0.5 transition-colors hover:bg-accent/40 hover:text-foreground"
        title="Copy this message text to the clipboard"
      >
        <Copy className="h-3 w-3" />
        Copy
      </button>
    </div>
  );
}
