import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useApp } from "@/store/app";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { api } from "@/lib/api";
import {
  Trash2,
  Copy,
  Languages,
  Sparkles,
  FileText,
  Code2,
  ListTree,
  MessageSquare,
  Inbox,
} from "lucide-react";
import { cn, formatRelative } from "@/lib/utils";
import { toast } from "sonner";

const ACTION_META: Record<
  string,
  { icon: React.ReactNode; label: string; tone: string }
> = {
  translate: { icon: <Languages className="h-3 w-3" />, label: "Translation", tone: "info" },
  clarify: { icon: <Sparkles className="h-3 w-3" />, label: "Clarify", tone: "default" },
  summarize: { icon: <FileText className="h-3 w-3" />, label: "Summary", tone: "muted" },
  explain: { icon: <Code2 className="h-3 w-3" />, label: "Explain code", tone: "default" },
  glossary: { icon: <ListTree className="h-3 w-3" />, label: "Glossary", tone: "muted" },
  comment: { icon: <MessageSquare className="h-3 w-3" />, label: "Comment", tone: "warning" },
};

export function Scratchpad() {
  const open = useApp((s) => s.scratchpadOpen);
  const session = useApp((s) => s.session);
  const items = useApp((s) => s.scratchpadItems);
  const setItems = useApp((s) => s.setScratchpadItems);
  const removeItem = useApp((s) => s.removeScratchpadItem);

  React.useEffect(() => {
    if (!open) return;
    api
      .scratchpadList(session?.meta.session_id ?? undefined)
      .then(setItems)
      .catch(console.error);
  }, [open, session?.meta.session_id, setItems]);

  if (!open) return null;

  return (
    <aside className="flex h-full w-96 shrink-0 flex-col border-l border-border bg-card/50">
      <div className="flex h-9 shrink-0 items-center justify-between border-b border-border px-3">
        <div className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Scratchpad
        </div>
        <Badge variant="muted" className="font-mono">
          {items.length}
        </Badge>
      </div>
      <ScrollArea className="flex-1 scrollbar-thin">
        <div className="space-y-2 p-2">
          {items.length === 0 && (
            <div className="flex flex-col items-center gap-2 p-6 text-center text-xs text-muted-foreground">
              <Inbox className="h-6 w-6 opacity-40" />
              Select text in the timeline and pick an action.
              <br />
              Translate, Clarify, Summarize, Explain code, Glossary, Comment.
            </div>
          )}
          {items.map((item) => (
            <ActionCard
              key={item.id}
              item={item}
              onDelete={async () => {
                try {
                  await api.scratchpadDelete(item.id);
                  removeItem(item.id);
                } catch {}
              }}
            />
          ))}
        </div>
      </ScrollArea>
    </aside>
  );
}

function ActionCard({
  item,
  onDelete,
}: {
  item: import("@/lib/api").ScratchpadItem;
  onDelete: () => void;
}) {
  const meta = ACTION_META[item.action] ?? {
    icon: <Sparkles className="h-3 w-3" />,
    label: item.action,
    tone: "default",
  };
  const isTranslation = item.action === "translate";
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(item.result);
      toast.success("Copied");
    } catch {}
  };
  return (
    <div className="rounded-md border border-border bg-background p-2.5 shadow-sm">
      <div className="mb-1.5 flex items-center gap-2 text-[11px]">
        <span className="flex h-5 items-center gap-1 rounded bg-muted px-1.5 font-medium">
          {meta.icon}
          {meta.label}
        </span>
        {item.model && item.model !== "user" && (
          <span className="font-mono text-muted-foreground">
            {item.model.replace(/^claude-/, "")}
          </span>
        )}
        <span className="ms-auto text-muted-foreground">
          {formatRelative(item.created_at)}
        </span>
        <button
          onClick={copy}
          className="text-muted-foreground transition-colors hover:text-foreground"
          title="Copy"
        >
          <Copy className="h-3 w-3" />
        </button>
        <button
          onClick={onDelete}
          className="text-muted-foreground transition-colors hover:text-destructive"
          title="Delete"
        >
          <Trash2 className="h-3 w-3" />
        </button>
      </div>
      {item.source_text && (
        <blockquote dir="auto" className="mb-2 truncate border-s-2 border-border ps-2 text-[11px] italic text-muted-foreground">
          {item.source_text.slice(0, 220)}
          {item.source_text.length > 220 ? "…" : ""}
        </blockquote>
      )}
      <div
        dir={isTranslation ? "rtl" : "auto"}
        className={cn("prose-msg text-[12.5px]", isTranslation && "font-arabic")}
      >
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{item.result}</ReactMarkdown>
      </div>
    </div>
  );
}
