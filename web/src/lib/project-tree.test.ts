import { describe, expect, it } from "vitest";
import { buildProjectTree, dedupeSessions } from "@/lib/project-tree";
import type { ProjectMeta, SessionMeta } from "@/lib/api";

function makeSession(overrides: Partial<SessionMeta> = {}): SessionMeta {
  return {
    session_id: "sess-1",
    bucket: "b",
    file_path: "/tmp/x.jsonl",
    size_bytes: 1000,
    line_count: null,
    modified_ms: 1000,
    cwd: "/foo",
    git_branch: null,
    entrypoint: null,
    version: null,
    ai_title: null,
    first_prompt: null,
    last_model: null,
    remote_name: null,
    ...overrides,
  };
}

function makeProject(overrides: Partial<ProjectMeta> = {}): ProjectMeta {
  return {
    bucket: "b",
    display_name: "B",
    cwd: "/foo",
    last_modified_ms: 100,
    session_count: 0,
    sessions: [],
    ...overrides,
  };
}

describe("buildProjectTree", () => {
  it("merges two projects with the same cwd into a single node", () => {
    const a = makeProject({
      bucket: "ssh-aaa",
      cwd: "/home/u/proj",
      sessions: [makeSession({ session_id: "s1", file_path: "/a.jsonl" })],
    });
    const b = makeProject({
      bucket: "ssh-bbb",
      cwd: "/home/u/proj",
      sessions: [makeSession({ session_id: "s2", file_path: "/b.jsonl" })],
    });
    const tree = buildProjectTree([a, b]);
    expect(tree).toHaveLength(1);
    expect(tree[0].project.sessions.map((s) => s.session_id).sort()).toEqual([
      "s1",
      "s2",
    ]);
  });

  it("dedupes same session_id across sources, keeping the larger file", () => {
    const a = makeProject({
      bucket: "ssh-cache",
      cwd: "/home/u/proj",
      sessions: [
        makeSession({ session_id: "shared", size_bytes: 100, file_path: "/old.jsonl" }),
      ],
    });
    const b = makeProject({
      bucket: "remote:host:bucket",
      cwd: "/home/u/proj",
      sessions: [
        makeSession({ session_id: "shared", size_bytes: 5000, file_path: "/new.jsonl" }),
      ],
    });
    const tree = buildProjectTree([a, b]);
    expect(tree).toHaveLength(1);
    expect(tree[0].project.sessions).toHaveLength(1);
    expect(tree[0].project.sessions[0].size_bytes).toBe(5000);
    expect(tree[0].project.sessions[0].file_path).toBe("/new.jsonl");
  });

  it("nests subfolder cwd under the parent", () => {
    const parent = makeProject({ bucket: "p", cwd: "/foo" });
    const child = makeProject({ bucket: "c", cwd: "/foo/bar" });
    const tree = buildProjectTree([parent, child]);
    expect(tree).toHaveLength(1);
    expect(tree[0].project.cwd).toBe("/foo");
    expect(tree[0].children).toHaveLength(1);
    expect(tree[0].children[0].project.cwd).toBe("/foo/bar");
    expect(tree[0].children[0].depth).toBe(1);
    expect(tree[0].children[0].relativeName).toBe("bar");
  });

  it("detects worktree paths under .claude/worktrees", () => {
    const wt = makeProject({
      bucket: "wt",
      cwd: "/foo/.claude/worktrees/feature-x",
    });
    const tree = buildProjectTree([wt]);
    expect(tree).toHaveLength(1);
    expect(tree[0].isWorktree).toBe(true);
  });
});

describe("dedupeSessions", () => {
  it("keeps the larger file when sizes differ", () => {
    const small = makeSession({ session_id: "s", size_bytes: 100 });
    const large = makeSession({ session_id: "s", size_bytes: 9999 });
    const out = dedupeSessions([small, large]);
    expect(out).toHaveLength(1);
    expect(out[0].size_bytes).toBe(9999);
  });

  it("breaks size ties by modified_ms (newer wins)", () => {
    const older = makeSession({ session_id: "s", size_bytes: 500, modified_ms: 1 });
    const newer = makeSession({ session_id: "s", size_bytes: 500, modified_ms: 2 });
    const out = dedupeSessions([older, newer]);
    expect(out).toHaveLength(1);
    expect(out[0].modified_ms).toBe(2);
  });
});
