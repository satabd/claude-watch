"""Live watching of remote SSH hosts.

For each enabled remote we keep one persistent SSH connection and poll the
remote's `~/.claude/projects/` tree for changes. New bytes are tail-fetched
into the local mirror dir, where the existing watchdog observer picks them up
and emits SSE events — the same pipeline used for native local sessions.

Reliability features:
- Config-change detection: when the user edits a host (port, key, etc.) the
  manager restarts that watcher; an unchanged config is left alone.
- Idle backoff: poll cadence stretches from 2s up to 10s when nothing on the
  remote is changing, then snaps back to 2s the moment activity is detected.
- Status visibility: the watcher writes its current state to the DB
  (`status`, `last_poll_ms`, `last_event_ms`, `next_retry_ms`) so the UI can
  render a live badge per host.
- Backoff visibility: while reconnecting, the next attempt timestamp is
  exposed and surfaced in the status string.
- Truncation/rotation handling: if the remote file shrinks, we replace the
  local mirror fully and the local byte-offset tailer resets to 0. Both
  layers log a warning.
- Partial line handling: deferred to the local watchdog tailer (single source
  of truth for split-on-newline). It rewinds the offset when a chunk lacks a
  trailing newline, so the next read picks up the completed line.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

import asyncssh

from . import db, remotes

log = logging.getLogger("watcher.remote")


# ---- Tunables ------------------------------------------------------------
#
# All four below are overridable via environment variables, with the defaults
# below in parentheses. Tradeoffs:
#
# WATCHER_REMOTE_ACTIVE_POLL_S (default 2s)
#     How often we re-stat known files when activity is detected.
#     Lower = faster live updates but more SFTP traffic per minute.
#     Higher = quieter network, slower turn rendering.
#
# WATCHER_REMOTE_IDLE_POLL_MAX_S (default 10s)
#     Upper bound when the remote has been quiet for several poll cycles.
#     We ramp from ACTIVE → IDLE_MAX over IDLE_RAMP_AFTER_POLLS, then snap
#     back to ACTIVE the moment any file changes.
#     Lower = quicker first-event latency after a quiet stretch, but more
#       polling overhead during quiet periods.
#     Higher = less idle traffic; first event after a long quiet period can
#       lag by up to this many seconds.
#
# WATCHER_REMOTE_IDLE_RAMP_POLLS (default 10)
#     Number of consecutive no-change polls before we start stretching the
#     interval toward the idle max.
#
# WATCHER_REMOTE_FULL_SCAN_S (default 30s)
#     How often we re-listdir every bucket to catch newly-created sessions
#     that are NOT yet in our `_known` set. New sessions surface within this
#     interval, then get incremental polling from then on.
#     Lower = newly-started remote sessions appear in the watcher faster,
#       at the cost of one extra listdir-per-bucket every cycle.
#     Higher = lower network load, but new sessions can take this long to
#       become visible.
#
# Environment variables are read once at module import. Restart the server
# to pick up changes.


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = float(raw)
        return v if v > 0 else default
    except ValueError:
        log.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except ValueError:
        log.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


ACTIVE_POLL_INTERVAL: float = _env_float("WATCHER_REMOTE_ACTIVE_POLL_S", 2.0)
IDLE_POLL_INTERVAL: float = _env_float("WATCHER_REMOTE_IDLE_POLL_MAX_S", 10.0)
IDLE_RAMP_AFTER_POLLS: int = _env_int("WATCHER_REMOTE_IDLE_RAMP_POLLS", 10)
FULL_SCAN_INTERVAL: float = _env_float("WATCHER_REMOTE_FULL_SCAN_S", 30.0)

# Reconnect backoff is hard-coded; very short failures shouldn't be drowned
# out and very long ones shouldn't hang forever.
RECONNECT_BACKOFF_INITIAL = 3.0
RECONNECT_BACKOFF_MAX = 60.0


log.info(
    "remote-watcher tunables: active=%.1fs idle_max=%.1fs ramp_after=%d full_scan=%.1fs",
    ACTIVE_POLL_INTERVAL,
    IDLE_POLL_INTERVAL,
    IDLE_RAMP_AFTER_POLLS,
    FULL_SCAN_INTERVAL,
)

# Distinct from None so _set_status callers can explicitly pass None to clear
# a field (e.g. last_error=None) versus omit it (leave unchanged).
_UNSET: Any = object()


def host_config_signature(host: dict[str, Any]) -> tuple:
    """Fields the SSH connection cares about. If any change, restart the watcher."""
    return (
        host.get("host"),
        host.get("port"),
        host.get("username"),
        host.get("key_path"),
        host.get("projects_path"),
        # Toggling enabled is handled separately in reload(), but include it
        # for completeness (a 0→1 transition should also restart).
        bool(host.get("enabled")),
    )


# ---- one watcher per host ----


class RemoteWatcher:
    """Background tailer for one remote SSH host."""

    def __init__(self, host: dict[str, Any]):
        self.host = host
        self.host_id: int = host["id"]
        self.host_name: str = host["name"]
        self.signature = host_config_signature(host)
        self.task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # rel_path ("bucket/file.jsonl") -> (size, mtime) of last known remote state
        self._known: dict[str, tuple[int, int]] = {}
        # Status the manager / UI can read
        self._status: str = "starting"

    @property
    def projects_path(self) -> str:
        p = self.host.get("projects_path")
        if p:
            return p
        home = self.host.get("home_dir")
        if home:
            return f"{home.rstrip('/')}/.claude/projects"
        return "~/.claude/projects"

    def start(self) -> None:
        """Idempotent: starting an already-running watcher is a no-op."""
        if self.task and not self.task.done():
            return
        self._stop.clear()
        self._set_status("starting")
        self.task = asyncio.create_task(
            self._run(), name=f"remote-watcher-{self.host_name}"
        )

    async def stop(self) -> None:
        """Cancel + wait for clean exit. Connection / SFTP closes via async-with."""
        self._stop.set()
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.task = None
        self._set_status("stopped")

    # ---- main loop ----

    async def _run(self) -> None:
        delay = RECONNECT_BACKOFF_INITIAL
        while not self._stop.is_set():
            try:
                await self._connect_and_tail()
                if self._stop.is_set():
                    return
                # Loop body returned cleanly without an error — reset backoff.
                delay = RECONNECT_BACKOFF_INITIAL
            except asyncio.CancelledError:
                raise
            except (asyncssh.Error, OSError, asyncio.TimeoutError) as e:
                msg = f"{type(e).__name__}: {e}"
                next_at = int((time.time() + delay) * 1000)
                log.warning(
                    "remote %s connection error (%s); next retry in %.1fs",
                    self.host_name, msg, delay,
                )
                self._set_status(f"reconnecting in {int(delay)}s", error=msg, next_retry_ms=next_at)
                if await self._sleep_or_stop(delay):
                    return
                delay = min(delay * 2, RECONNECT_BACKOFF_MAX)
            except Exception as e:  # noqa: BLE001
                log.exception("remote %s unexpected error", self.host_name)
                msg = f"{type(e).__name__}: {e}"
                next_at = int((time.time() + delay) * 1000)
                self._set_status(f"reconnecting in {int(delay)}s", error=msg, next_retry_ms=next_at)
                if await self._sleep_or_stop(delay):
                    return
                delay = min(delay * 2, RECONNECT_BACKOFF_MAX)

    async def _connect_and_tail(self) -> None:
        self._set_status("connecting", next_retry_ms=None)
        async with await remotes._connect(self.host) as conn:
            sftp = await conn.start_sftp_client()
            try:
                # Initial full scan establishes baseline + downloads anything missing.
                changed = await self._full_scan(sftp)
                self._record_poll(changed=changed)
                self._set_status("live")
                last_full = time.monotonic()
                idle_polls = 0

                while not self._stop.is_set():
                    interval = self._compute_interval(idle_polls)
                    if await self._sleep_or_stop(interval):
                        return

                    now = time.monotonic()
                    if now - last_full > FULL_SCAN_INTERVAL:
                        changed = await self._full_scan(sftp)
                        last_full = now
                    else:
                        changed = await self._incremental_poll(sftp)

                    self._record_poll(changed=changed)
                    if changed:
                        idle_polls = 0
                    else:
                        idle_polls += 1
            finally:
                sftp.exit()

    @staticmethod
    def _compute_interval(idle_polls: int) -> float:
        """2s when active; ramp toward IDLE_POLL_INTERVAL when quiet."""
        if idle_polls < IDLE_RAMP_AFTER_POLLS:
            return ACTIVE_POLL_INTERVAL
        # Linear ramp from active → idle interval over ~5 more polls
        excess = idle_polls - IDLE_RAMP_AFTER_POLLS
        ratio = min(1.0, excess / 5)
        return ACTIVE_POLL_INTERVAL + (IDLE_POLL_INTERVAL - ACTIVE_POLL_INTERVAL) * ratio

    async def _sleep_or_stop(self, seconds: float) -> bool:
        """Sleep up to `seconds`, returning True if stop was requested."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    # ---- scans ----

    async def _full_scan(self, sftp: asyncssh.SFTPClient) -> bool:
        """List every bucket and stat every JSONL inside.

        Returns True if any file changed (new or grew).
        """
        try:
            buckets = await sftp.listdir(self.projects_path)
        except (asyncssh.SFTPError, OSError) as e:
            log.warning("remote %s: cannot list %s: %s", self.host_name, self.projects_path, e)
            return False

        local_root = remotes.host_mirror_dir(self.host_name)
        local_root.mkdir(parents=True, exist_ok=True)
        any_changed = False

        # Forget known files for buckets that disappeared remotely so we don't
        # keep statting them.
        seen_buckets = {b for b in buckets if b not in (".", "..")}
        for rel in list(self._known.keys()):
            bucket_part = rel.split("/", 1)[0]
            if bucket_part not in seen_buckets:
                self._known.pop(rel, None)

        for bucket in seen_buckets:
            bucket_remote = f"{self.projects_path}/{bucket}"
            try:
                files = await sftp.listdir(bucket_remote)
            except (asyncssh.SFTPError, OSError):
                continue
            for fname in files:
                if not fname.endswith(".jsonl"):
                    continue
                if await self._sync_file(sftp, bucket, fname):
                    any_changed = True
        return any_changed

    async def _incremental_poll(self, sftp: asyncssh.SFTPClient) -> bool:
        """Re-stat already-known files and fetch any growth.

        Returns True if any file changed.
        """
        any_changed = False
        for rel in list(self._known.keys()):
            try:
                bucket, fname = rel.rsplit("/", 1)
            except ValueError:
                continue
            if await self._sync_file(sftp, bucket, fname):
                any_changed = True
        return any_changed

    # ---- per-file sync ----

    async def _sync_file(self, sftp: asyncssh.SFTPClient, bucket: str, fname: str) -> bool:
        """Returns True if the file actually changed on remote (size or mtime)."""
        rel = f"{bucket}/{fname}"
        remote_path = f"{self.projects_path}/{rel}"
        try:
            rstat = await sftp.stat(remote_path)
        except (asyncssh.SFTPError, OSError):
            return False
        rsize = int(rstat.size or 0)
        rmtime = int(rstat.mtime or 0)

        prev = self._known.get(rel)
        if prev and prev == (rsize, rmtime):
            return False  # unchanged

        local_root = remotes.host_mirror_dir(self.host_name)
        local_path = local_root / bucket / fname
        local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            local_size = local_path.stat().st_size if local_path.exists() else 0
        except OSError:
            local_size = 0

        # File truncation / rotation: remote shrunk relative to local. Replace
        # fully — the local watchdog tailer also resets its offset when it sees
        # the file got smaller, so the SSE stream stays consistent.
        if local_path.exists() and rsize < local_size:
            log.warning(
                "remote %s: %s shrank (local=%d, remote=%d) — likely truncated/rotated; replacing",
                self.host_name, rel, local_size, rsize,
            )
            try:
                await sftp.get(remote_path, str(local_path))
            except (asyncssh.SFTPError, OSError, asyncio.TimeoutError) as e:
                log.debug("full get %s failed: %s", rel, e)
                return False
        elif local_path.exists() and (prev is None or local_size == prev[0]):
            # Append-only fetch: just the new bytes. Partial line at the very
            # end is fine — the local tailer rewinds and waits for the next chunk.
            try:
                await self._append_fetch(sftp, remote_path, local_path, local_size, rsize)
            except (asyncssh.SFTPError, OSError, asyncio.TimeoutError) as e:
                log.debug("append-fetch %s failed (%s); falling back to full get", rel, e)
                try:
                    await sftp.get(remote_path, str(local_path))
                except Exception:  # noqa: BLE001
                    return False
        else:
            # New file or local got out of sync — replace fully.
            try:
                await sftp.get(remote_path, str(local_path))
            except (asyncssh.SFTPError, OSError, asyncio.TimeoutError) as e:
                log.debug("full get %s failed: %s", rel, e)
                return False

        # Preserve mtime so the next stat-compare can detect quiet periods.
        try:
            os.utime(local_path, (rmtime, rmtime))
        except OSError:
            pass
        self._known[rel] = (rsize, rmtime)
        return True

    async def _append_fetch(
        self,
        sftp: asyncssh.SFTPClient,
        remote_path: str,
        local_path: Path,
        start_offset: int,
        total_size: int,
    ) -> None:
        if total_size <= start_offset:
            return
        async with sftp.open(remote_path, "rb") as rf:
            await rf.seek(start_offset)
            chunk = await rf.read()
        with local_path.open("ab") as lf:
            lf.write(chunk)

    # ---- status bookkeeping ----

    def _set_status(
        self,
        status: str,
        *,
        error: Any = _UNSET,
        next_retry_ms: Any = _UNSET,
    ) -> None:
        if status == self._status and error is _UNSET and next_retry_ms is _UNSET:
            return
        self._status = status
        fields: dict[str, Any] = {"status": status}
        if error is not _UNSET:
            fields["last_error"] = error
        if next_retry_ms is not _UNSET:
            fields["next_retry_ms"] = next_retry_ms
        try:
            db.update_remote_host(self.host_id, **fields)
        except Exception:  # noqa: BLE001
            pass

    def _record_poll(self, *, changed: bool) -> None:
        now_ms = int(time.time() * 1000)
        fields: dict[str, Any] = {
            "last_poll_ms": now_ms,
            "last_synced_ms": now_ms,  # back-compat with earlier UI code
            "last_error": None,
            "next_retry_ms": None,
        }
        if changed:
            fields["last_event_ms"] = now_ms
        try:
            db.update_remote_host(self.host_id, **fields)
        except Exception:  # noqa: BLE001
            pass


# ---- manager: orchestrates one watcher per enabled host ----


class RemoteWatcherManager:
    def __init__(self) -> None:
        self.watchers: dict[int, RemoteWatcher] = {}
        self._lock = asyncio.Lock()

    async def start_all(self) -> None:
        async with self._lock:
            for host in db.list_remote_hosts():
                if host.get("enabled"):
                    self._start_locked(host)
                else:
                    self._mark_disabled(host)

    async def stop_all(self) -> None:
        async with self._lock:
            for w in list(self.watchers.values()):
                await w.stop()
            self.watchers.clear()

    async def reload(self) -> None:
        """Recompute the active set from the DB.

        - Disabled / deleted hosts: cancel + close their watcher.
        - Newly-enabled hosts: start.
        - Hosts whose connection-relevant config changed (host/port/key/etc):
          stop the old watcher and start a fresh one.
        - Hosts whose config is unchanged: leave the running watcher alone.
        """
        async with self._lock:
            current = {h["id"]: h for h in db.list_remote_hosts() if h.get("enabled")}
            # Stop watchers no longer enabled
            for host_id in list(self.watchers.keys()):
                if host_id not in current:
                    w = self.watchers.pop(host_id)
                    await w.stop()
                    log.info("remote watcher stopped for host_id=%s", host_id)
            # Start / restart as needed
            for host_id, host in current.items():
                existing = self.watchers.get(host_id)
                if existing is None:
                    self._start_locked(host)
                elif existing.signature != host_config_signature(host):
                    log.info(
                        "remote %s config changed; restarting watcher", host["name"]
                    )
                    await existing.stop()
                    self.watchers.pop(host_id, None)
                    self._start_locked(host)
                # else: same config, leave running

            # Surface "disabled" status for hosts that aren't running
            for host in db.list_remote_hosts():
                if not host.get("enabled") and host["id"] not in self.watchers:
                    self._mark_disabled(host)

    def _start_locked(self, host: dict[str, Any]) -> None:
        w = RemoteWatcher(host)
        self.watchers[host["id"]] = w
        w.start()
        log.info(
            "remote watcher started for %s (host_id=%s)", host["name"], host["id"]
        )

    @staticmethod
    def _mark_disabled(host: dict[str, Any]) -> None:
        try:
            db.update_remote_host(
                host["id"], status="disabled", next_retry_ms=None
            )
        except Exception:  # noqa: BLE001
            pass
