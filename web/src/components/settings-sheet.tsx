import * as React from "react";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useApp } from "@/store/app";
import { api } from "@/lib/api";
import { toast } from "sonner";
import { Bot, Cpu, Loader2 } from "lucide-react";
import { RemotesManager } from "@/components/remotes/remotes-manager";
import { cn } from "@/lib/utils";

const PROVIDER_INFO: Record<
  string,
  { label: string; description: string; tagline: string; icon: React.ReactNode }
> = {
  claude: {
    label: "Claude (Haiku 4.5)",
    description: "Uses your `claude` CLI OAuth. Reliable, supports Arabic well. ~13–17s per translation.",
    tagline: "subscription · slower",
    icon: <Bot className="h-4 w-4" />,
  },
  codex: {
    label: "ChatGPT (Codex CLI)",
    description: "Uses your ChatGPT subscription via `codex exec` with low reasoning effort. ~8–11s per translation. Requires codex-cli ≥ 0.128 (`npm install -g @openai/codex`).",
    tagline: "ChatGPT · ~35% faster",
    icon: <Cpu className="h-4 w-4" />,
  },
};

export function SettingsSheet() {
  const open = useApp((s) => s.settingsOpen);
  const setOpen = useApp((s) => s.setSettingsOpen);
  const settings = useApp((s) => s.settings);
  const setSettings = useApp((s) => s.setSettings);
  const [busy, setBusy] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (open && !settings) {
      api.getSettings().then(setSettings).catch(console.error);
    }
  }, [open, settings, setSettings]);

  React.useEffect(() => {
    // Load on mount so the topbar can show the active provider badge
    api.getSettings().then(setSettings).catch(() => {});
  }, [setSettings]);

  const switchProvider = async (provider: string) => {
    if (busy || settings?.provider === provider) return;
    setBusy(provider);
    try {
      const next = await api.updateSettings({ provider });
      setSettings(next);
      toast.success(`Provider: ${PROVIDER_INFO[provider]?.label ?? provider}`);
    } catch (e: any) {
      toast.error(e?.message ?? "Failed to update");
    } finally {
      setBusy(null);
    }
  };

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetContent className="flex h-full flex-col overflow-y-auto scrollbar-thin sm:max-w-md">
        <SheetHeader>
          <SheetTitle>Settings</SheetTitle>
          <SheetDescription>
            Pick which CLI handles translate / clarify / summarize / explain / glossary.
            Cached translations are reused regardless of provider.
          </SheetDescription>
        </SheetHeader>

        <div className="mt-5">
          <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
            LLM provider
          </div>
          <div className="space-y-2">
            {settings?.available_providers.map((p) => {
              const info = PROVIDER_INFO[p] ?? {
                label: p,
                description: "",
                tagline: "",
                icon: <Bot className="h-4 w-4" />,
              };
              const active = settings.provider === p;
              return (
                <button
                  key={p}
                  onClick={() => switchProvider(p)}
                  disabled={busy !== null}
                  className={cn(
                    "group relative w-full rounded-md border p-3 text-left transition-colors",
                    active
                      ? "border-primary bg-primary/5 shadow-sm"
                      : "border-border hover:border-foreground/30 hover:bg-accent/30"
                  )}
                >
                  <div className="flex items-start gap-3">
                    <div
                      className={cn(
                        "flex h-7 w-7 shrink-0 items-center justify-center rounded-md",
                        active ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground"
                      )}
                    >
                      {busy === p ? <Loader2 className="h-4 w-4 animate-spin" /> : info.icon}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="text-[13px] font-medium">{info.label}</span>
                        {active && <Badge variant="success">active</Badge>}
                      </div>
                      <div className="mt-0.5 text-[11px] text-muted-foreground">{info.tagline}</div>
                      <div className="mt-1.5 text-[12px] leading-snug text-muted-foreground">
                        {info.description}
                      </div>
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        <div className="mt-6">
          <RemotesManager />
        </div>

        <div className="mt-5 rounded-md border border-border bg-muted/30 p-3 text-[12px] text-muted-foreground">
          <div className="mb-1 font-medium text-foreground">Tips</div>
          <ul className="list-disc space-y-1 ps-4">
            <li>Translations are cached forever in <code className="font-mono text-[11px]">~/.claude/watcher/cache.sqlite</code>.</li>
            <li>If Codex rejects models, run <code className="font-mono text-[11px]">npm install -g @openai/codex</code>.</li>
            <li>Remote sync downloads to <code className="font-mono text-[11px]">~/.claude/watcher/remotes/&lt;host&gt;/</code>. Click <b>Sync now</b> after a remote session ends to pull updates.</li>
          </ul>
        </div>

        <div className="mt-5 flex justify-end">
          <Button variant="outline" size="sm" onClick={() => setOpen(false)}>
            Close
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
