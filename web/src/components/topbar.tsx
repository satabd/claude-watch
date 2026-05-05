import { useApp } from "@/store/app";
import { Button } from "@/components/ui/button";
import { Eye, Moon, Sun, PanelRightOpen, PanelRightClose, Settings, Bot, Cpu } from "lucide-react";
import { shortPath } from "@/lib/utils";
import { sessionDisplayName } from "@/lib/session-display";

export function Topbar() {
  const session = useApp((s) => s.session);
  const scratchpadOpen = useApp((s) => s.scratchpadOpen);
  const toggleScratchpad = useApp((s) => s.toggleScratchpad);
  const theme = useApp((s) => s.theme);
  const toggleTheme = useApp((s) => s.toggleTheme);
  const settings = useApp((s) => s.settings);
  const setSettingsOpen = useApp((s) => s.setSettingsOpen);

  const meta = session?.meta;

  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b border-border bg-background/95 px-3">
      <div className="flex items-center gap-2">
        <div className="flex h-7 w-7 items-center justify-center rounded-md bg-primary text-primary-foreground">
          <Eye className="h-3.5 w-3.5" />
        </div>
        <div className="text-sm font-semibold tracking-tight">Claude Watcher</div>
      </div>

      <div className="mx-2 h-5 w-px bg-border" />

      {meta ? (
        <div className="flex min-w-0 flex-1 items-center gap-3 text-xs">
          <span
            className="min-w-0 truncate text-sm font-medium text-foreground"
            title={meta.ai_title ?? undefined}
          >
            {sessionDisplayName(meta, 90)}
          </span>
          {meta.cwd && (
            <span
              className="shrink min-w-0 truncate font-mono text-[11px] text-muted-foreground"
              title={meta.cwd}
            >
              {shortPath(meta.cwd, 50)}
            </span>
          )}
        </div>
      ) : (
        <div className="flex-1 text-xs text-muted-foreground">No session selected</div>
      )}

      <div className="flex items-center gap-1.5">
        <button
          onClick={() => setSettingsOpen(true)}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1 text-[11px] font-medium transition-colors hover:bg-accent"
          title="Change LLM provider"
        >
          {settings?.provider === "codex" ? <Cpu className="h-3 w-3" /> : <Bot className="h-3 w-3" />}
          {settings?.provider === "codex" ? "ChatGPT" : "Claude"}
        </button>
        <Button variant="ghost" size="icon" onClick={() => setSettingsOpen(true)} title="Settings">
          <Settings />
        </Button>
        <Button variant="ghost" size="icon" onClick={toggleTheme} title="Toggle theme">
          {theme === "dark" ? <Sun /> : <Moon />}
        </Button>
        <Button
          variant="ghost"
          size="icon"
          onClick={toggleScratchpad}
          title="Toggle scratchpad (Cmd-J)"
        >
          {scratchpadOpen ? <PanelRightClose /> : <PanelRightOpen />}
        </Button>
      </div>
    </header>
  );
}
