import type { ProjectMeta } from "@/lib/api";

export interface ProjectNode {
  project: ProjectMeta;
  children: ProjectNode[];
  /** Relative-to-parent path for nicer display (set when nested). */
  relativeName?: string;
  /** Path-segment depth in the tree (root = 0). */
  depth: number;
  /** True if cwd contains `/.claude/worktrees/` — render as a worktree. */
  isWorktree: boolean;
}

const SEPARATOR_RE = /[\\/]/;

function normalize(p: string): string {
  // Lower-case drive letter on Windows so D:\foo and d:\foo match
  if (p.length >= 2 && p[1] === ":") {
    return p[0].toLowerCase() + p.slice(1).replace(/\\/g, "/");
  }
  return p.replace(/\\/g, "/");
}

function isPathPrefix(parent: string, child: string): boolean {
  const np = normalize(parent);
  const nc = normalize(child);
  if (nc === np) return false;
  if (!nc.startsWith(np)) return false;
  const next = nc[np.length];
  return next === "/" || next === "\\";
}

function relativeFrom(parent: string, child: string): string {
  const np = normalize(parent);
  const nc = normalize(child);
  let rel = nc.slice(np.length);
  while (rel.startsWith("/") || rel.startsWith("\\")) rel = rel.slice(1);
  return rel;
}

/** Merge projects that share the same normalized cwd into one virtual project.
 *  Common cases:
 *    1. Multiple SSH buckets to the same remote machine (Claude Desktop creates
 *       a fresh ssh-<uuid> bucket per pairing).
 *    2. The same session appearing both as a stale local ssh-* cache AND as a
 *       fresh remote-mirror copy. We keep one copy per session_id and prefer
 *       the larger / more recent file (almost always the remote mirror).
 */
function mergeByCwd(projects: ProjectMeta[]): ProjectMeta[] {
  const byCwd: Map<string, ProjectMeta> = new Map();
  const out: ProjectMeta[] = [];
  for (const p of projects) {
    if (!p.cwd) {
      out.push(p);
      continue;
    }
    const key = normalize(p.cwd);
    const existing = byCwd.get(key);
    if (!existing) {
      const clone: ProjectMeta = { ...p, sessions: [...p.sessions] };
      byCwd.set(key, clone);
      out.push(clone);
    } else {
      existing.sessions.push(...p.sessions);
      existing.last_modified_ms = Math.max(
        existing.last_modified_ms,
        p.last_modified_ms
      );
    }
  }
  // Dedupe by session_id within each merged project, then sort newest-first.
  for (const p of out) {
    p.sessions = dedupeSessions(p.sessions);
    p.sessions.sort((a, b) => b.modified_ms - a.modified_ms);
    p.session_count = p.sessions.length;
  }
  return out;
}

/** When the same session_id appears twice (e.g. local ssh-* cache + remote
 *  mirror), keep the one whose file is largest, then most-recently modified.
 *  Remote-mirror copies almost always win since we sync the latest bytes. */
export function dedupeSessions(sessions: ProjectMeta["sessions"]): ProjectMeta["sessions"] {
  const seen = new Map<string, ProjectMeta["sessions"][number]>();
  for (const s of sessions) {
    const prev = seen.get(s.session_id);
    if (!prev) {
      seen.set(s.session_id, s);
      continue;
    }
    // Prefer the larger / newer file.
    const winner =
      s.size_bytes !== prev.size_bytes
        ? s.size_bytes > prev.size_bytes
          ? s
          : prev
        : s.modified_ms > prev.modified_ms
        ? s
        : prev;
    seen.set(s.session_id, winner);
  }
  return Array.from(seen.values());
}

/** Build a parent/child tree from a flat list of projects, by cwd path-prefix. */
export function buildProjectTree(projects: ProjectMeta[]): ProjectNode[] {
  projects = mergeByCwd(projects);
  // Roots first (no cwd), then by cwd length ascending so parents are processed before children.
  const sorted = [...projects].sort((a, b) => {
    if (!a.cwd && b.cwd) return -1;
    if (a.cwd && !b.cwd) return 1;
    return (a.cwd?.length ?? 0) - (b.cwd?.length ?? 0);
  });

  const nodesByCwd: Map<string, ProjectNode> = new Map();
  const roots: ProjectNode[] = [];

  for (const p of sorted) {
    const node: ProjectNode = {
      project: p,
      children: [],
      depth: 0,
      isWorktree: !!p.cwd && /[\\/]\.claude[\\/]worktrees[\\/]/i.test(p.cwd),
    };

    if (p.cwd) {
      // Find longest cwd that is a path-prefix of this one
      let bestParent: ProjectNode | null = null;
      let bestLen = -1;
      for (const [parentCwd, parentNode] of nodesByCwd) {
        if (isPathPrefix(parentCwd, p.cwd) && parentCwd.length > bestLen) {
          bestParent = parentNode;
          bestLen = parentCwd.length;
        }
      }
      if (bestParent) {
        node.relativeName = relativeFrom(bestParent.project.cwd!, p.cwd);
        node.depth = bestParent.depth + 1;
        bestParent.children.push(node);
      } else {
        roots.push(node);
      }
      nodesByCwd.set(p.cwd, node);
    } else {
      roots.push(node);
    }
  }

  // Sort each level by most-recent activity
  const sortByActivity = (a: ProjectNode, b: ProjectNode) =>
    b.project.last_modified_ms - a.project.last_modified_ms;
  const recur = (n: ProjectNode) => {
    n.children.sort(sortByActivity);
    n.children.forEach(recur);
  };
  roots.sort(sortByActivity);
  roots.forEach(recur);

  return roots;
}
