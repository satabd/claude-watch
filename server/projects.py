"""Discover Claude Code projects/buckets and sessions."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

PROJECTS_ROOT = Path.home() / ".claude" / "projects"
# Mirror dir for remote-fetched sessions, keyed by remote name then bucket name.
REMOTES_ROOT = Path.home() / ".claude" / "watcher" / "remotes"


@dataclass
class SessionMeta:
    session_id: str
    bucket: str
    file_path: str
    size_bytes: int
    line_count: int | None
    modified_ms: int
    cwd: str | None
    git_branch: str | None
    entrypoint: str | None
    version: str | None
    ai_title: str | None
    first_prompt: str | None = None
    last_model: str | None = None
    remote_name: str | None = None  # set when this session lives in a remote mirror


@dataclass
class ProjectMeta:
    bucket: str
    display_name: str
    cwd: str | None
    sessions: list[SessionMeta]
    last_modified_ms: int


def _decode_bucket(bucket: str) -> str:
    """Best-effort decode of bucket folder name back to a cwd-ish display label.

    The encoding scheme replaces : and / and \\ with -. Reversal is ambiguous, so
    we use it only for display fallback when we don't have cwd from JSONL.
    """
    if bucket.startswith("ssh-"):
        return f"remote ({bucket[4:12]})"
    s = bucket
    # First "--" likely separates drive from path on Windows
    if len(s) >= 2 and s[1:3] == "--":
        s = s[0] + ":/" + s[3:].replace("-", "/")
    else:
        s = s.replace("-", "/")
    return s


def _read_first_meta_line(path: Path) -> dict | None:
    """Read JSONL file and return the first event line that has a cwd field."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("cwd"):
                    return obj
                # Even if no cwd, capture metadata-bearing lines
                if obj.get("type") in ("user", "assistant", "attachment", "system"):
                    return obj
    except OSError:
        return None
    return None


def _read_ai_title(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "ai-title":
                    return obj.get("aiTitle")
    except OSError:
        return None
    return None


def _read_first_prompt_and_last_model(path: Path) -> tuple[str | None, str | None]:
    """Return the first non-command user prompt (truncated) and the most-recent
    assistant model seen in the session. Cheap single-pass scan."""
    first_prompt: str | None = None
    last_model: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                if first_prompt is None and t == "user":
                    msg = obj.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, str):
                        s = content.strip()
                        # Skip command artifacts (/model, /clear, etc.)
                        if s and not s.startswith("<"):
                            first_prompt = s[:200]
                if t == "assistant":
                    msg = obj.get("message") or {}
                    m = msg.get("model")
                    if m:
                        last_model = m
    except OSError:
        return first_prompt, last_model
    return first_prompt, last_model


def session_meta(file_path: Path, remote_name: str | None = None) -> SessionMeta:
    stat = file_path.stat()
    meta = _read_first_meta_line(file_path) or {}
    title = _read_ai_title(file_path)
    first_prompt, last_model = _read_first_prompt_and_last_model(file_path)
    bucket_name = file_path.parent.name
    # When the caller didn't say, infer whether this file lives in a
    # remote mirror — REMOTES_ROOT/<host>/<bucket>/<file>.jsonl. The
    # GET /api/sessions/{bucket}/{id} route resolves a path via
    # find_session() without passing remote_name; without this the
    # response bucket would drop the "remote:<host>:" prefix that
    # GET /api/projects advertised, and the frontend would sit on a
    # permanent selection/meta mismatch ("Switching session…" forever).
    if remote_name is None:
        try:
            parts = file_path.relative_to(REMOTES_ROOT).parts
        except ValueError:
            parts = ()
        if len(parts) >= 3:
            remote_name = parts[0]
    # Namespace remote bucket names so they don't collide with local ones
    if remote_name:
        bucket_name = f"remote:{remote_name}:{bucket_name}"
    return SessionMeta(
        session_id=file_path.stem,
        bucket=bucket_name,
        file_path=str(file_path),
        size_bytes=stat.st_size,
        line_count=None,
        modified_ms=int(stat.st_mtime * 1000),
        cwd=meta.get("cwd"),
        git_branch=meta.get("gitBranch"),
        entrypoint=meta.get("entrypoint"),
        version=meta.get("version"),
        ai_title=title,
        first_prompt=first_prompt,
        last_model=last_model,
        remote_name=remote_name,
    )


def _scan_root(root: Path, remote_name: str | None = None) -> list[ProjectMeta]:
    """Scan one project-root directory (local or one remote mirror)."""
    if not root.exists():
        return []
    out: list[ProjectMeta] = []
    for bucket_dir in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not bucket_dir.is_dir():
            continue
        files = sorted(
            bucket_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not files:
            continue
        sessions = [session_meta(f, remote_name=remote_name) for f in files]
        cwd = sessions[0].cwd if sessions else None
        display = cwd if cwd else _decode_bucket(bucket_dir.name)
        last_mod = max((s.modified_ms for s in sessions), default=0)
        bucket_name = bucket_dir.name
        if remote_name:
            bucket_name = f"remote:{remote_name}:{bucket_name}"
        out.append(
            ProjectMeta(
                bucket=bucket_name,
                display_name=display,
                cwd=cwd,
                sessions=sessions,
                last_modified_ms=last_mod,
            )
        )
    return out


def list_projects() -> list[ProjectMeta]:
    projects: list[ProjectMeta] = list(_scan_root(PROJECTS_ROOT))

    # Each remote has its own mirror dir directly under REMOTES_ROOT
    if REMOTES_ROOT.exists():
        for host_dir in REMOTES_ROOT.iterdir():
            if not host_dir.is_dir():
                continue
            projects.extend(_scan_root(host_dir, remote_name=host_dir.name))

    projects.sort(key=lambda p: p.last_modified_ms, reverse=True)
    return projects


def find_session(bucket: str, session_id: str) -> Path | None:
    if bucket.startswith("remote:"):
        # remote:<host_name>:<original_bucket>
        try:
            _, host_name, original = bucket.split(":", 2)
        except ValueError:
            return None
        p = REMOTES_ROOT / host_name / original / f"{session_id}.jsonl"
        return p if p.exists() else None
    p = PROJECTS_ROOT / bucket / f"{session_id}.jsonl"
    return p if p.exists() else None
