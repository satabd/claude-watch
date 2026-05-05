import * as React from "react";
import { useApp } from "@/store/app";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Turn } from "./turn";
import { SelectionToolbar } from "./selection-toolbar";
import { SessionToolbar } from "./session-toolbar";
import type { ToolResult } from "@/lib/api";
import { ArrowDown, ArrowUp, Eye, Search, Globe } from "lucide-react";
import { cn } from "@/lib/utils";

export function Timeline() {
  const session = useApp((s) => s.session);
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

  if (!session) {
    return <NoSessionSelected />;
  }

  const q = filterQuery.trim().toLowerCase();
  const visible = session.events.filter((e) => {
    if (e.is_command_artifact) return false;
    if (e.type === "user") {
      if (!e.user_text) return false;
      if (!filterRoles.user) return false;
    } else if (e.type === "assistant") {
      if (!(e.text_blocks.length > 0 || e.tool_uses.length > 0)) return false;
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
