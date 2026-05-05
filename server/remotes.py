"""SSH/SFTP-backed remote project sync.

For each configured host, we connect via SSH, locate `~/.claude/projects/`,
and mirror new-or-changed JSONL files to a local directory:

    ~/.claude/watcher/remotes/<host_name>/<bucket>/<session_id>.jsonl

The existing project listing + parser then picks them up just like local
sessions, with `bucket` rewritten as `remote:<host_name>:<bucket>` so they
sort separately and stay identifiable.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncssh

# Where remote files get mirrored to. The projects scanner reads this too.
REMOTES_ROOT = Path.home() / ".claude" / "watcher" / "remotes"
REMOTES_ROOT.mkdir(parents=True, exist_ok=True)

# Reasonable connection / per-operation timeouts (seconds).
CONNECT_TIMEOUT = 15
OP_TIMEOUT = 60


@dataclass
class SyncReport:
    host_name: str
    ok: bool
    error: str | None = None
    home_dir: str | None = None
    platform: str | None = None
    discovered_buckets: int = 0
    files_seen: int = 0
    files_downloaded: int = 0
    files_unchanged: int = 0
    bytes_downloaded: int = 0
    elapsed_ms: int = 0
    detail: list[str] = field(default_factory=list)


def _safe_name(s: str) -> str:
    """Sanitize a host name for filesystem use (kept short and slug-like)."""
    out = []
    for ch in s.strip():
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        else:
            out.append("-")
    return "".join(out).strip("-") or "host"


def host_mirror_dir(host_name: str) -> Path:
    """Filesystem location where this host's mirrored JSONLs live."""
    return REMOTES_ROOT / _safe_name(host_name)


async def _connect(host: dict[str, Any]) -> asyncssh.SSHClientConnection:
    import socket

    # Normalize "localhost" to 127.0.0.1: asyncssh prefers IPv6 (::1) when given
    # a hostname, but WSL2's localhost forwarding is IPv4-only.
    h = host["host"]
    if h.lower() in ("localhost",):
        h = "127.0.0.1"
    kwargs: dict[str, Any] = {
        "host": h,
        "port": host.get("port", 22),
        "username": host["username"],
        "known_hosts": None,  # for local lab use; documented as caveat
        "connect_timeout": CONNECT_TIMEOUT,
        "family": socket.AF_INET,  # prefer IPv4 — friendlier across WSL/Docker/etc
        # Detect dead connections quickly: 30s ping, drop after 3 missed.
        "keepalive_interval": 30,
        "keepalive_count_max": 3,
    }
    key = host.get("key_path")
    if key:
        kwargs["client_keys"] = [os.path.expanduser(os.path.expandvars(key))]
    # Otherwise asyncssh tries the user's ssh-agent + default key locations
    return await asyncssh.connect(**kwargs)


async def _detect_paths(
    conn: asyncssh.SSHClientConnection, override: str | None
) -> tuple[str, str | None, str | None]:
    """Return (projects_path, home_dir, platform). Detect home + platform.

    Tries Linux/macOS/WSL first; falls back to a Windows-style probe.
    """
    home_dir: str | None = None
    platform: str | None = None
    try:
        r = await asyncio.wait_for(
            conn.run('echo "$HOME" 2>/dev/null && uname -s 2>/dev/null', check=False),
            timeout=OP_TIMEOUT,
        )
        out = (r.stdout or "").strip().splitlines()
        if out and out[0].startswith("/"):
            home_dir = out[0]
            platform = (out[1] if len(out) > 1 else "unknown").strip()
    except Exception:
        pass

    # Windows fallback
    if not home_dir:
        try:
            r = await asyncio.wait_for(
                conn.run('echo %USERPROFILE%', check=False),
                timeout=OP_TIMEOUT,
            )
            out = (r.stdout or "").strip().splitlines()
            if out:
                line = out[0]
                if line and not line.startswith("%"):
                    home_dir = line.replace("\\", "/")
                    platform = "Windows"
        except Exception:
            pass

    if override:
        projects_path = override
    elif home_dir:
        # Use forward slashes; SFTP handles both on Windows OpenSSH.
        projects_path = home_dir.rstrip("/").rstrip("\\") + "/.claude/projects"
    else:
        projects_path = "~/.claude/projects"

    return projects_path, home_dir, platform


async def test_connection(host: dict[str, Any]) -> dict[str, Any]:
    """Verify auth + locate ~/.claude/projects/. Returns a status dict."""
    try:
        async with await _connect(host) as conn:
            projects_path, home_dir, platform = await _detect_paths(
                conn, host.get("projects_path")
            )
            sftp = await conn.start_sftp_client()
            try:
                exists = False
                bucket_count = 0
                try:
                    entries = await sftp.listdir(projects_path)
                    exists = True
                    # Filter out . and ..
                    bucket_count = sum(
                        1 for e in entries if e not in (".", "..")
                    )
                except (asyncssh.SFTPError, OSError):
                    exists = False
                return {
                    "ok": True,
                    "home_dir": home_dir,
                    "platform": platform,
                    "projects_path": projects_path,
                    "projects_exists": exists,
                    "bucket_count": bucket_count,
                }
            finally:
                sftp.exit()
    except (asyncssh.Error, OSError, asyncio.TimeoutError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def sync_host(host: dict[str, Any]) -> SyncReport:
    """Mirror remote ~/.claude/projects/ JSONLs to the local cache.

    Only re-downloads files whose remote (size, mtime) differ from the local
    mirror's. Returns a report with counts.
    """
    started = asyncio.get_event_loop().time()
    report = SyncReport(host_name=host["name"], ok=False)

    try:
        async with await _connect(host) as conn:
            projects_path, home_dir, platform = await _detect_paths(
                conn, host.get("projects_path")
            )
            report.home_dir = home_dir
            report.platform = platform

            sftp = await conn.start_sftp_client()
            try:
                try:
                    bucket_names = await sftp.listdir(projects_path)
                except (asyncssh.SFTPError, OSError) as e:
                    raise RuntimeError(
                        f"cannot list {projects_path}: {e}. "
                        "Set projects_path explicitly if it differs."
                    )

                bucket_names = [b for b in bucket_names if b not in (".", "..")]
                report.discovered_buckets = len(bucket_names)
                local_root = host_mirror_dir(host["name"])
                local_root.mkdir(parents=True, exist_ok=True)

                for bucket in bucket_names:
                    remote_bucket = f"{projects_path}/{bucket}"
                    try:
                        files = await sftp.listdir(remote_bucket)
                    except (asyncssh.SFTPError, OSError):
                        continue

                    jsonls = [f for f in files if f.endswith(".jsonl")]
                    if not jsonls:
                        continue

                    local_bucket = local_root / bucket
                    local_bucket.mkdir(parents=True, exist_ok=True)

                    for fname in jsonls:
                        report.files_seen += 1
                        remote_path = f"{remote_bucket}/{fname}"
                        local_path = local_bucket / fname
                        try:
                            rstat = await sftp.stat(remote_path)
                        except (asyncssh.SFTPError, OSError):
                            continue
                        rsize = int(rstat.size or 0)
                        rmtime = int(rstat.mtime or 0)
                        # Skip if local is up-to-date
                        if local_path.exists():
                            try:
                                lstat = local_path.stat()
                                if (
                                    int(lstat.st_size) == rsize
                                    and int(lstat.st_mtime) == rmtime
                                ):
                                    report.files_unchanged += 1
                                    continue
                            except OSError:
                                pass
                        # Download
                        try:
                            await asyncio.wait_for(
                                sftp.get(remote_path, str(local_path)),
                                timeout=OP_TIMEOUT * 4,
                            )
                            report.files_downloaded += 1
                            report.bytes_downloaded += rsize
                            # Preserve mtime so the next diff catches no-op
                            try:
                                os.utime(local_path, (rmtime, rmtime))
                            except OSError:
                                pass
                        except (asyncssh.SFTPError, OSError, asyncio.TimeoutError) as e:
                            report.detail.append(f"failed {bucket}/{fname}: {e}")

                # Clean up local buckets/files that no longer exist on remote.
                # (Optional v1: skip — keeps the mirror as a forever cache.)
                report.ok = True
            finally:
                sftp.exit()
    except (asyncssh.Error, OSError, asyncio.TimeoutError) as e:
        report.ok = False
        report.error = f"{type(e).__name__}: {e}"
    except RuntimeError as e:
        report.ok = False
        report.error = str(e)
    finally:
        report.elapsed_ms = int(
            (asyncio.get_event_loop().time() - started) * 1000
        )
    return report


def remove_host_mirror(host_name: str) -> int:
    """Delete a host's mirrored files. Returns the number of files removed."""
    d = host_mirror_dir(host_name)
    if not d.exists():
        return 0
    n = sum(1 for _ in d.rglob("*.jsonl"))
    shutil.rmtree(d, ignore_errors=True)
    return n
