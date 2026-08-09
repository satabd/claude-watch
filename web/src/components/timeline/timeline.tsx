import * as React from "react";
import { useApp } from "@/store/app";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Turn } from "./turn";
import { Composer } from "./composer";
import { SelectionToolbar } from "./selection-toolbar";
import { SessionToolbar } from "./session-toolbar";
import type { ToolResult } from "@/lib/api";
import { ArrowDown, ArrowUp, Eye, Search, Globe } from "lucide-react";
import { cn } from "@/lib/utils";

export function Timeline() {
  const session = useApp((s) => s.session);
  const selectedBucket = useApp((s) => s.selectedBucket);
  const selectedSessionId = useApp((s) => s.selectedSessionId);
  const sessionLoading = useApp((s) => s.sessionLoading);
  const filterQuery = useApp((s) => s.filterQuery);
  const filterRoles = useApp((s) => s.filterRoles);
  const filterTool = useApp((s) => s.filterTool);
  const containerRef = React.useRef<HTMLDivElement>(null);
  const scrollRef = React.useRef<HTMLDivElement>(null);
  const lastEventCount = React.useRef(0);
  const [scrollPos, setScrollPos] = React.useState<{ atTop: boolean; atBottom: boolean; newSinceAway: number }>({
    atTop: true,
    atBottom: true,
    newSinceAway: 0,
  });
  const newSinceAwayRef = React.useRef(0);

  // Pair tool_use → tool_result via tool_use_id, scanning the whole stream
  const toolResultsById: Record<string, ToolResult> = React.useMemo(() => {
    const map: Record<string, ToolResult> = {};
    if (!session) return map;
    for (const ev of session.events) {
      for (const r of ev.tool_results) {
        if (r.tool_use_id) map[r.tool_use_id] = r;
      }
    }
    return map;
  }, [session]);

  const getViewport = React.useCallback(() => {
    return scrollRef.current?.querySelector<HTMLElement>(
      "[data-radix-scroll-area-viewport]"
    );
  }, []);

  // Auto-scroll to bottom when new events arrive (if we were already near bottom).
  // Otherwise, count them as "new since away" so the floating button can badge them.
  React.useEffect(() => {
    const events = session?.events ?? [];
    const sc = getViewport();
    if (!sc) {
      lastEventCount.current = events.length;
      return;
    }
    const wasNear = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 200;
    if (events.length > lastEventCount.current) {
      const delta = events.length - lastEventCount.current;
      if (wasNear) {
        requestAnimationFrame(() => {
          sc.scrollTop = sc.scrollHeight;
        });
        newSinceAwayRef.current = 0;
        setScrollPos((p) => ({ ...p, newSinceAway: 0 }));
      } else {
        newSinceAwayRef.current += delta;
        setScrollPos((p) => ({ ...p, newSinceAway: newSinceAwayRef.current }));
      }
    }
    lastEventCount.current = events.length;
  }, [session?.events.length, getViewport]);

  // Reset counters when switching sessions
  React.useEffect(() => {
    newSinceAwayRef.current = 0;
    setScrollPos({ atTop: true, atBottom: true, newSinceAway: 0 });
  }, [session?.meta.session_id]);

  // Track scroll position via rAF polling (Radix's viewport doesn't always
  // bubble scroll events reliably across re-mounts). The polling sets
  // disabled state on the buttons and clears the new-message badge when
  // the user lands near the bottom.
  React.useEffect(() => {
    let raf = 0;
    const tick = () => {
      const sc = getViewport();
      if (sc) {
        const distanceFromBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight;
        const atTop = sc.scrollTop < 50;
        const atBottom = distanceFromBottom < 50;
        if (atBottom && newSinceAwayRef.current > 0) {
          newSinceAwayRef.current = 0;
          setScrollPos({ atTop, atBottom, newSinceAway: 0 });
        } else {
          setScrollPos((p) =>
            p.atTop === atTop &&
            p.atBottom === atBottom &&
            p.newSinceAway === newSinceAwayRef.current
              ? p
              : { atTop, atBottom, newSinceAway: newSinceAwayRef.current }
          );
        }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [getViewport]);

  const scrollToBottom = React.useCallback(() => {
    const sc = getViewport();
    if (!sc) return;
    sc.scrollTo({ top: sc.scrollHeight, behavior: "smooth" });
    newSinceAwayRef.current = 0;
    setScrollPos((p) => ({ ...p, newSinceAway: 0 }));
  }, [getViewport]);

  const scrollToTop = React.useCallback(() => {
    const sc = getViewport();
    if (!sc) return;
    sc.scrollTo({ top: 0, behavior: "smooth" });
  }, [getViewport]);

  // No selection at all → friendly start screen.
  if (!selectedBucket || !selectedSessionId) {
    return <NoSessionSelected />;
  }
  // A selection is set but the session hasn't arrived yet, OR a different
  // session is currently displayed (mid-transition). Render a skeleton so
  // the user knows something is on its way and isn't confused by stale
  // content from the previously-loaded session. The fall-through below
  // narrows `session` to non-null for the rest of the function.
  if (
    !session ||
    session.meta.bucket !== selectedBucket ||
    session.meta.session_id !== selectedSessionId
  ) {
    return <TimelineSkeleton loading={sessionLoading || !session} />;
  }

  const q = filterQuery.trim().toLowerCase();
  const visible = session.events.filter((e) => {
    if (e.is_command_artifact) return false;
    if (e.type === "user") {
      if (!e.user_text) return false;
      if (!filterRoles.user) return false;
    } else if (e.type === "assistant") {
      if (
        !(
          e.text_blocks.length > 0 ||
          e.tool_uses.length > 0 ||
          e.thinking_blocks.length > 0
        )
      )
        return false;
      if (!filterRoles.assistant) return false;
    } else if (e.type === "pr-link") {
      // pr-link rides with assistant role
      if (!filterRoles.assistant) return false;
    } else {
      return false;
    }
    if (filterTool && !e.tool_uses.some((t) => t.name === filterTool)) return false;
    if (q) {
      const haystack = (
        (e.user_text || "") +
        " " +
        e.text_blocks.join(" ") +
        " " +
        e.thinking_blocks.join(" ") +
        " " +
        e.tool_uses
          .map((t) => `${t.name} ${JSON.stringify(t.input || {})}`)
          .join(" ")
      ).toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    return true;
  });

  return (
    <div ref={containerRef} className="relative flex h-full min-w-0 flex-1 flex-col">
      <ScrollArea ref={scrollRef} className="flex-1 scrollbar-thin">
        <div className="mx-auto max-w-4xl pb-24">
          <SessionToolbar />
          {visible.map((ev) => (
            <Turn key={ev.uuid} event={ev} toolResultsById={toolResultsById} />
          ))}
          {visible.length === 0 && (
            <div className="p-8 text-center text-sm text-muted-foreground">
              This session has no rendered messages yet.
            </div>
          )}
          {selectedBucket && selectedSessionId && (
            <Composer bucket={selectedBucket} sessionId={selectedSessionId} />
          )}
        </div>
      </ScrollArea>
      <SelectionToolbar containerRef={containerRef} />
      <ScrollNavButtons
        atTop={scrollPos.atTop}
        atBottom={scrollPos.atBottom}
        newSinceAway={scrollPos.newSinceAway}
        onTop={scrollToTop}
        onBottom={scrollToBottom}
      />
    </div>
  );
}

function ScrollNavButtons({
  atTop,
  atBottom,
  newSinceAway,
  onTop,
  onBottom,
}: {
  atTop: boolean;
  atBottom: boolean;
  newSinceAway: number;
  onTop: () => void;
  onBottom: () => void;
}) {
  return (
    <div className="absolute bottom-4 end-4 z-20 flex flex-col gap-2">
      <button
        onClick={onTop}
        disabled={atTop}
        className={cn(
          "flex h-9 w-9 items-center justify-center rounded-full border border-border bg-background/90 shadow-md transition-colors",
          atTop
            ? "cursor-default text-muted-foreground/40 opacity-50"
            : "text-muted-foreground hover:bg-accent hover:text-foreground"
        )}
        title="Scroll to top"
      >
        <ArrowUp className="h-4 w-4" />
      </button>
      <button
        onClick={onBottom}
        disabled={atBottom && newSinceAway === 0}
        className={cn(
          "relative flex h-9 items-center justify-center rounded-full border bg-background/90 px-2.5 shadow-md transition-colors",
          newSinceAway > 0
            ? "border-emerald-500/50 text-emerald-600 hover:bg-emerald-500/10 dark:text-emerald-300"
            : atBottom
            ? "cursor-default border-border text-muted-foreground/40 opacity-50 w-9"
            : "border-border text-muted-foreground hover:bg-accent hover:text-foreground w-9"
        )}
        title={
          newSinceAway > 0
            ? `${newSinceAway} new ${newSinceAway === 1 ? "message" : "messages"} — scroll to bottom`
            : "Scroll to bottom"
        }
      >
        <ArrowDown className="h-4 w-4" />
        {newSinceAway > 0 && (
          <span className="ms-1 text-[11px] font-semibold tabular-nums">
            {newSinceAway > 99 ? "99+" : newSinceAway}
          </span>
        )}
      </button>
    </div>
  );
}

/** Lightweight skeleton shown while the selected session is being fetched.
 *  Mirrors the rough rhythm of the real timeline (alternating user/assistant
 *  blocks) so the layout doesn't reflow on arrival. */
function TimelineSkeleton({ loading }: { loading: boolean }) {
  return (
    <div
      role="status"
      aria-label="Loading session"
      className="flex flex-1 flex-col"
    >
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 p-6 motion-safe:animate-pulse">
        <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-muted-foreground">
          <div className="h-1.5 w-1.5 rounded-full bg-muted-foreground/40" />
          {loading ? "Loading session…" : "Switching session…"}
        </div>
        {[
          { role: "user", lines: 2 },
          { role: "assistant", lines: 4 },
          { role: "user", lines: 1 },
          { role: "assistant", lines: 3 },
        ].map((row, i) => (
          <div
            key={i}
            className={cn(
              "space-y-2 rounded-md border border-border/40 p-3",
              row.role === "user" ? "bg-muted/20" : "bg-card/40"
            )}
          >
            <div className="h-2.5 w-24 rounded bg-muted/60" />
            {Array.from({ length: row.lines }).map((_, j) => (
              <div
                key={j}
                className={cn(
                  "h-2.5 rounded bg-muted/50",
                  j === row.lines - 1 ? "w-2/3" : "w-full"
                )}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function NoSessionSelected() {
  const projects = useApp((s) => s.projects);
  const setSettingsOpen = useApp((s) => s.setSettingsOpen);
  const hasProjects = projects.length > 0;
  return (
    <div className="flex flex-1 flex-col items-center justify-center p-8 text-center">
      <div className="max-w-md space-y-4 text-muted-foreground">
        <div className="flex flex-col items-center gap-2">
          <Eye className="h-8 w-8 opacity-30" />
          <div className="text-sm font-medium text-foreground">
            {hasProjects ? "Select a session to view it" : "No sessions yet"}
          </div>
        </div>
        <ul className="space-y-1.5 text-left text-xs leading-relaxed">
          <li className="flex items-start gap-2">
            <span className="mt-[6px] inline-block h-1 w-1 shrink-0 rounded-full bg-muted-foreground/50" />
            <span>
              Pick any session from the sidebar to load its full transcript.
            </span>
          </li>
          <li className="flex items-start gap-2">
            <Search className="mt-[2px] h-3 w-3 shrink-0 opacity-60" />
            <span>
              Use the search box and role / tool filters in the toolbar to narrow
              long sessions.
            </span>
          </li>
          <li className="flex items-start gap-2">
            <Globe className="mt-[2px] h-3 w-3 shrink-0 opacity-60" />
            <span>
              Sessions running on WSL or a remote VPS? Add the host once and the
              live watcher will mirror them here.
            </span>
          </li>
        </ul>
        <div className="flex justify-center pt-1">
          <Button
            variant="outline"
            size="sm"
            className="h-7"
            onClick={() => setSettingsOpen(true)}
          >
            <Globe className="h-3 w-3" /> Add remote host
          </Button>
        </div>
      </div>
    </div>
  );
}
