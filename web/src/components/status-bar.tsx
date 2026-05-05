import * as React from "react";
import {
  Activity,
  Cpu,
  Folder,
  GitBranch,
  Coins,
  Hash,
  Terminal,
  Zap,
  Copy,
  Check,
} from "lucide-react";
import { useApp } from "@/store/app";
import { computeSessionStats, fmtCost, fmtTokens } from "@/lib/session-stats";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export function StatusBar() {
  const session = useApp((s) => s.session);
  const sseConnected = useApp((s) => s.sseConnected);
  const settings = useApp((s) => s.settings);
  const stats = React.useMemo(() => computeSessionStats(session), [session]);

  const meta = session?.meta;
  const sessionId = meta?.session_id ?? null;
  const cwdName = meta?.cwd?.split(/[\\/]/).filter(Boolean).pop() ?? null;
  const dominantModel =
    stats.dominantModel ?? meta?.last_model ?? null;
  const dominantShort = dominantModel?.replace(/^claude-/, "") ?? null;

  const [copied, setCopied] = React.useState(false);
  const copySessionId = async () => {
    if (!sessionId) return;
    try {
      await navigator.clipboard.writeText(sessionId);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {}
  };

  return (
    <footer className="flex h-6 shrink-0 items-center gap-0 border-t border-border bg-card/80 px-1 font-mono text-[11px] text-muted-foreground">
      <TooltipProvider delayDuration={150}>
        {/* Left cluster — project / branch / entrypoint / id */}
        <Section>
          {cwdName && (
            <Item icon={<Folder className="h-3 w-3" />} label={cwdName} title={meta?.cwd ?? ""} />
          )}
          {meta?.git_branch && (
            <Item
              icon={<GitBranch className="h-3 w-3" />}
              label={meta.git_branch}
              title={`git branch: ${meta.git_branch}`}
            />
          )}
          {meta?.entrypoint && (
            <Item
              icon={<Terminal className="h-3 w-3" />}
              label={meta.entrypoint}
              title={`entrypoint: ${meta.entrypoint}`}
            />
          )}
          {sessionId && (
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  onClick={copySessionId}
                  className="flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-accent hover:text-foreground"
                >
                  <Hash className="h-3 w-3" />
                  <span className="tabular-nums">{sessionId.slice(0, 8)}</span>
                  {copied ? (
                    <Check className="h-3 w-3 text-emerald-500" />
                  ) : (
                    <Copy className="h-3 w-3 opacity-50" />
                  )}
                </button>
              </TooltipTrigger>
              <TooltipContent>
                <div>{sessionId}</div>
                <div className="text-[10px] opacity-70">click to copy</div>
              </TooltipContent>
            </Tooltip>
          )}
        </Section>

        {/* Right cluster — turns / tokens / cost / model / live */}
        <div className="ms-auto flex h-full items-center">
          {session && (
            <Section>
              <Item
                icon={<span className="font-mono text-[11px]">∑</span>}
                label={`${stats.visibleTurns} turn${stats.visibleTurns === 1 ? "" : "s"}`}
                title="Visible turns in this session"
              />
              <Tooltip>
                <TooltipTrigger asChild>
                  <button className="flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-accent hover:text-foreground">
                    <Zap className="h-3 w-3" />
                    <span className="tabular-nums">
                      {fmtTokens(stats.outputTokens)} out
                    </span>
                  </button>
                </TooltipTrigger>
                <TooltipContent>
                  <div className="space-y-0.5 text-[11px] leading-tight">
                    <div>Output: {stats.outputTokens.toLocaleString()}</div>
                    <div>Input (fresh): {stats.inputTokens.toLocaleString()}</div>
                    <div>Cache write: {stats.cacheCreateTokens.toLocaleString()}</div>
                    <div>
                      Cache read: {stats.cacheReadTokens.toLocaleString()} (
                      {stats.cacheHitPct.toFixed(0)}% hit)
                    </div>
                  </div>
                </TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <button className="flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-accent hover:text-foreground">
                    <Coins className="h-3 w-3" />
                    <span className="tabular-nums">{fmtCost(stats.costEstimate)}</span>
                  </button>
                </TooltipTrigger>
                <TooltipContent>
                  <div className="space-y-0.5 text-[11px] leading-tight">
                    <div>Estimated cost (USD)</div>
                    {stats.costIsFallback && (
                      <div className="text-amber-400">
                        Using sonnet pricing as fallback (non-Anthropic dominant model)
                      </div>
                    )}
                    <div className="opacity-70">
                      Excludes provider markups & subscription cost
                    </div>
                  </div>
                </TooltipContent>
              </Tooltip>
              {dominantShort && (
                <Item
                  icon={<Cpu className="h-3 w-3" />}
                  label={dominantShort}
                  title={`Dominant model: ${dominantModel}`}
                />
              )}
            </Section>
          )}

          <Section>
            {settings?.provider && (
              <Item
                icon={settings.provider === "codex" ? (
                  <Cpu className="h-3 w-3" />
                ) : (
                  <Cpu className="h-3 w-3" />
                )}
                label={settings.provider === "codex" ? "ChatGPT" : "Claude"}
                title={`Active provider: ${settings.provider}`}
              />
            )}
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  className={cn(
                    "flex items-center gap-1 rounded px-1.5 py-0.5",
                    sseConnected ? "text-emerald-500" : "text-amber-500"
                  )}
                >
                  <Activity
                    className={cn("h-3 w-3", sseConnected && "live-dot")}
                  />
                  {sseConnected ? "live" : "reconnecting"}
                </span>
              </TooltipTrigger>
              <TooltipContent>
                {sseConnected
                  ? "SSE connected — live updates flowing"
                  : "SSE disconnected — retrying"}
              </TooltipContent>
            </Tooltip>
          </Section>
        </div>
      </TooltipProvider>
    </footer>
  );
}

function Section({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full items-center divide-x divide-border/60">
      {React.Children.map(children, (c, i) => (
        <div key={i} className="flex h-full items-center px-0.5">
          {c}
        </div>
      ))}
    </div>
  );
}

function Item({
  icon,
  label,
  title,
}: {
  icon: React.ReactNode;
  label: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className="flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-accent hover:text-foreground"
    >
      {icon}
      <span className="truncate max-w-[14ch]">{label}</span>
    </span>
  );
}
