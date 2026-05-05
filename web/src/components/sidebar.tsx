import * as React from "react";
import { useApp } from "@/store/app";
import { ScrollArea } from "@/components/ui/scroll-area";
import { api, type SessionMeta } from "@/lib/api";
import { buildProjectTree, type ProjectNode } from "@/lib/project-tree";
import { sessionDisplayName } from "@/lib/session-display";
import { cn, formatBytes, formatRelative, shortPath } from "@/lib/utils";
import {
  ChevronRight,
  Folder,
  FolderOpen,
  Globe,
  Terminal,
  Cpu,
  GitBranch,
  FolderTree,
  Inbox,
} from "lucide-react";

export function Sidebar() {
  const projects = useApp((s) => s.projects);
  const setProjects = useApp((s) => s.setProjects);
  const selectedBucket = useApp((s) => s.selectedBucket);
  const selectedSessionId = useApp((s) => s.selectedSessionId);
  const selectSession = useApp((s) => s.selectSession);
  const setSettingsOpen = useApp((s) => s.setSettingsOpen);
  const [loaded, setLoaded] = React.useState(false);

  React.useEffect(() => {
    api
      .listProjects()
      .then(setProjects)
      .catch(console.error)
      .finally(() => setLoaded(true));
    const t = setInterval(
      () => api.listProjects().then(setProjects).catch(() => {}),
      30_000,
    );
    return () => clearInterval(t);
  }, [setProjects]);

  const tree = React.useMemo(() => buildProjectTree(projects), [projects]);

  return (
    <aside className="flex h-full w-72 shrink-0 flex-col border-r border-border bg-card/40">
      <div className="flex h-9 shrink-0 items-center justify-between px-3 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        <span>Projects</span>
        <span className="font-mono text-muted-foreground/70">{projects.length}</span>
      </div>
      <ScrollArea className="flex-1 scrollbar-thin">
        <div className="space-y-0.5 p-1.5">
          {loaded && projects.length === 0 ? (
            <SidebarEmptyState onOpenSettings={() => setSettingsOpen(true)} />
          ) : (
            tree.map((node) => (
              <ProjectTreeNode
                key={node.project.bucket}
                node={node}
                selectedBucket={selectedBucket}
                selectedSessionId={selectedSessionId}
                onSelect={selectSession}
              />
            ))
          )}
        </div>
      </ScrollArea>
    </aside>
  );
}

function SidebarEmptyState({ onOpenSettings }: { onOpenSettings: () => void }) {
  return (
    <div className="m-2 rounded-md border border-dashed border-border bg-background/50 p-3 text-[11.5px] leading-relaxed text-muted-foreground">
      <div className="mb-1.5 flex items-center gap-1.5 text-foreground">
        <Inbox className="h-3.5 w-3.5 opacity-70" />
        <span className="text-[12px] font-medium">No sessions found yet</span>
      </div>
      <p className="mb-2">
        Claude Watcher reads from{" "}
        <code className="rounded bg-muted px-1 font-mono text-[10.5px]">
          ~/.claude/projects
        </code>
        . Start a Claude Code session in any project directory and it will
        appear here automatically.
      </p>
      <p>
        For sessions on WSL or a remote machine,{" "}
        <button
          type="button"
          onClick={onOpenSettings}
          className="font-medium text-primary underline-offset-2 hover:underline"
        >
          add a Remote SSH host
        </button>
        .
      </p>
    </div>
  );
}

interface NodeProps {
  node: ProjectNode;
  selectedBucket: string | null;
  selectedSessionId: string | null;
  onSelect: (b: string, s: string) => void;
}

function ProjectTreeNode({ node, selectedBucket, selectedSessionId, onSelect }: NodeProps) {
  const { project, children, isWorktree, relativeName, depth } = node;
  const isSshBucket = project.bucket.startsWith("ssh-");
  const isSelfSelected = selectedBucket === project.bucket;
  const containsSelected =
    isSelfSelected ||
    children.some((c) => containsSession(c, selectedBucket));

  // Auto-open if anything below is active or if this is a top-level project.
  const [open, setOpen] = React.useState<boolean>(depth === 0 || containsSelected);
  React.useEffect(() => {
    if (containsSelected) setOpen(true);
  }, [containsSelected]);

  const sessions = project.sessions.slice(0, 12);
  const hasChildren = children.length > 0;

  const displayName = relativeName ?? project.display_name;
  const Icon = isSshBucket ? Globe : isWorktree ? GitBranch : isSelfSelected ? FolderOpen : Folder;
  const iconColor = isSshBucket
    ? "text-sky-500"
    : isWorktree
    ? "text-amber-500"
    : isSelfSelected
    ? "text-primary"
    : "text-muted-foreground";

  return (
    <div className={cn("rounded-md", isSelfSelected && !hasChildren && "bg-accent/40")}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="group/h flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-[12px] font-medium hover:bg-accent/40"
        title={project.cwd ?? project.display_name}
      >
        <ChevronRight
          className={cn(
            "h-3 w-3 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90",
            !sessions.length && !hasChildren && "opacity-0"
          )}
        />
        <Icon className={cn("h-3.5 w-3.5 shrink-0", iconColor)} />
        <span className="min-w-0 flex-1 truncate">
          {shortPath(displayName, depth === 0 ? 28 : 30)}
        </span>
        {isWorktree && project.git_branch && (
          <span className="shrink-0 truncate font-mono text-[10px] text-amber-500/80">
            {project.git_branch}
          </span>
        )}
        {hasChildren && (
          <span
            className="shrink-0 rounded bg-muted/70 px-1 font-mono text-[10px] text-muted-foreground"
            title={`${children.length} nested`}
          >
            <FolderTree className="inline h-2.5 w-2.5" /> {children.length}
          </span>
        )}
      </button>

      {open && (
        <div className="space-y-0.5 pb-0.5 ps-3 pe-1">
          {/* This project's own sessions. Each session keeps its OWN bucket
              from the original source (local ssh-*, remote mirror, etc.) — we
              must use s.bucket for clicks so find_session resolves correctly
              after mergeByCwd has combined local + remote. */}
          {sessions.map((s, i) => {
            const isLive = i === 0 && Date.now() - s.modified_ms < 60_000;
            const active =
              selectedBucket === s.bucket && selectedSessionId === s.session_id;
            return (
              <SessionRow
                key={`${s.bucket}:${s.session_id}`}
                session={s}
                active={active}
                isLive={isLive}
                onClick={() => onSelect(s.bucket, s.session_id)}
              />
            );
          })}
          {project.sessions.length > sessions.length && (
            <div className="px-2 py-0.5 text-[10px] text-muted-foreground/70">
              + {project.sessions.length - sessions.length} more
            </div>
          )}
          {sessions.length === 0 && !hasChildren && (
            <div className="px-2 py-1 text-[11px] leading-relaxed text-muted-foreground/80">
              No visible sessions yet — start or resume a Claude session in this
              project and it will appear here.
            </div>
          )}

          {/* Recursive children: worktrees / subfolder projects */}
          {hasChildren && (
            <div className="mt-1 space-y-0.5 border-s border-border/60 ps-1">
              {children.map((child) => (
                <ProjectTreeNode
                  key={child.project.bucket}
                  node={child}
                  selectedBucket={selectedBucket}
                  selectedSessionId={selectedSessionId}
                  onSelect={onSelect}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function containsSession(node: ProjectNode, selectedBucket: string | null): boolean {
  if (!selectedBucket) return false;
  if (node.project.bucket === selectedBucket) return true;
  return node.children.some((c) => containsSession(c, selectedBucket));
}

function SessionRow({
  session,
  active,
  isLive,
  onClick,
}: {
  session: SessionMeta;
  active: boolean;
  isLive: boolean;
  onClick: () => void;
}) {
  const display = sessionDisplayName(session, 50);
  const tip = `${display}\n${session.session_id}\n${formatBytes(session.size_bytes)}${
    session.remote_name ? `\nremote: ${session.remote_name}` : ""
  }`;
  return (
    <button
      onClick={onClick}
      className={cn(
        "group flex w-full items-center gap-1.5 rounded px-2 py-1 text-left text-[11.5px] transition-colors",
        active
          ? "bg-primary/10 text-foreground"
          : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
      )}
      title={tip}
    >
      <EntryIcon entrypoint={session.entrypoint} />
      <span className="min-w-0 flex-1 truncate">{display}</span>
      {session.remote_name && (
        <span
          className="shrink-0 truncate rounded bg-sky-500/15 px-1 font-mono text-[10px] text-sky-600 dark:text-sky-300"
          title={`remote: ${session.remote_name}`}
        >
          {session.remote_name.slice(0, 10)}
        </span>
      )}
      <span className="ms-auto flex shrink-0 items-center gap-1.5">
        {isLive && <span className="live-dot h-1.5 w-1.5 rounded-full bg-emerald-500" />}
        <span className="text-[10px] tabular-nums text-muted-foreground/80">
          {formatRelative(session.modified_ms)}
        </span>
      </span>
    </button>
  );
}

function EntryIcon({ entrypoint }: { entrypoint: string | null }) {
  if (!entrypoint) return <span className="h-3 w-3" />;
  const ep = entrypoint.toLowerCase();
  if (ep.includes("desktop")) return <Cpu className="h-3 w-3 text-purple-400/80" />;
  if (ep.includes("cli")) return <Terminal className="h-3 w-3 text-emerald-400/80" />;
  return <Cpu className="h-3 w-3 text-muted-foreground/70" />;
}
