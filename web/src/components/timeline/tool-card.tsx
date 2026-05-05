import * as React from "react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { ChevronRight, Wrench, Terminal as TerminalIcon, FileText, Pencil, Search, Globe2 } from "lucide-react";
import type { ToolUse, ToolResult } from "@/lib/api";

const ICONS: Record<string, React.ReactNode> = {
  Bash: <TerminalIcon className="h-3 w-3" />,
  PowerShell: <TerminalIcon className="h-3 w-3" />,
  Read: <FileText className="h-3 w-3" />,
  Write: <Pencil className="h-3 w-3" />,
  Edit: <Pencil className="h-3 w-3" />,
  MultiEdit: <Pencil className="h-3 w-3" />,
  Grep: <Search className="h-3 w-3" />,
  Glob: <Search className="h-3 w-3" />,
  WebFetch: <Globe2 className="h-3 w-3" />,
  WebSearch: <Globe2 className="h-3 w-3" />,
};

function summary(use: ToolUse): string {
  const i = use.input ?? {};
  if (use.name === "Bash" || use.name === "PowerShell") {
    const c = (i.command ?? "").split("\n")[0];
    return c.slice(0, 200);
  }
  if (use.name === "Read" || use.name === "Write" || use.name === "Edit" || use.name === "MultiEdit") {
    return i.file_path ?? "";
  }
  if (use.name === "Grep" || use.name === "Glob") {
    return i.pattern ?? i.glob ?? "";
  }
  if (use.name === "WebFetch") return i.url ?? "";
  if (use.name === "WebSearch") return i.query ?? "";
  // generic
  try {
    return JSON.stringify(i).slice(0, 200);
  } catch {
    return "";
  }
}

function resultPreview(r: ToolResult | undefined): string {
  if (!r) return "";
  const c = r.content;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) {
    return c
      .map((b) => (typeof b === "string" ? b : b?.text || ""))
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

export function ToolCard({
  use,
  result,
}: {
  use: ToolUse;
  result?: ToolResult;
}) {
  const icon = ICONS[use.name] ?? <Wrench className="h-3 w-3" />;
  const sumText = summary(use);
  const resText = resultPreview(result);
  const isError = result?.is_error;
  return (
    <Collapsible className="group/tool my-1 rounded-md border border-border/60 bg-muted/40 text-[12px]">
      <CollapsibleTrigger className="flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-muted/70">
        <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground transition-transform group-data-[state=open]/tool:rotate-90" />
        <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded bg-background text-muted-foreground">
          {icon}
        </span>
        <span className="shrink-0 font-mono font-medium">{use.name}</span>
        <span className="truncate font-mono text-[11px] text-muted-foreground" title={sumText}>
          {sumText}
        </span>
        {isError && (
          <span className="ms-auto shrink-0 rounded bg-destructive/15 px-1.5 py-0.5 text-[10px] text-destructive">
            error
          </span>
        )}
      </CollapsibleTrigger>
      <CollapsibleContent className="border-t border-border/60 px-3 py-2">
        {use.input && (
          <details className="mb-2">
            <summary className="cursor-pointer text-[11px] text-muted-foreground">input</summary>
            <pre dir="ltr" className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded bg-zinc-950 p-2 font-mono text-[11px] text-zinc-100">
{JSON.stringify(use.input, null, 2)}
            </pre>
          </details>
        )}
        {resText && (
          <div>
            <div className="mb-1 text-[11px] text-muted-foreground">output</div>
            <pre dir="ltr" className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded bg-zinc-950 p-2 font-mono text-[11px] text-zinc-100 scrollbar-thin">
{resText.slice(0, 8000)}
{resText.length > 8000 ? "\n[... truncated ...]" : ""}
            </pre>
          </div>
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}
