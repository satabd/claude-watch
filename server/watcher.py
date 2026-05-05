"""Watch ~/.claude/projects/ for new content and broadcast events.

We observe filesystem changes via watchdog, but actual line emission uses
byte-offset tailing (each file is append-only JSONL).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import parser
from .projects import PROJECTS_ROOT, REMOTES_ROOT

log = logging.getLogger("watcher.local")


class Broadcaster:
    """Async broadcaster: subscribers receive events via per-listener queues."""

    def __init__(self) -> None:
        self._listeners: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._listeners.discard(q)

    def publish(self, payload: dict[str, Any]) -> None:
        if self._loop is None:
            return
        with self._lock:
            listeners = list(self._listeners)
        for q in listeners:
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, payload)
            except RuntimeError:
                pass


class JsonlTailer:
    """Track byte offsets per file; emit new parsed events on changes."""

    def __init__(self, broadcaster: Broadcaster) -> None:
        self.broadcaster = broadcaster
        self.offsets: dict[str, int] = {}
        self._lock = threading.Lock()

    def init_file(self, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        # On first sight, skip past the existing content (we serve history via REST).
        # But emit a "session-touched" notification so the frontend can refresh lists.
        with self._lock:
            self.offsets[str(path)] = size

    def force_full_read(self, path: Path) -> None:
        with self._lock:
            self.offsets[str(path)] = 0

    def consume_new(self, path: Path) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        key = str(path)
        with self._lock:
            offset = self.offsets.get(key, 0)
            if offset > stat.st_size:
                # File shrunk (rotated/truncated) — reset to 0
                log.warning(
                    "local tailer: %s shrank from offset=%d to size=%d; "
                    "resetting (likely truncation/rotation)",
                    path.name, offset, stat.st_size,
                )
                offset = 0
        try:
            with path.open("rb") as f:
                f.seek(offset)
                chunk = f.read()
        except OSError:
            return

        new_offset = offset + len(chunk)
        with self._lock:
            self.offsets[key] = new_offset

        if not chunk:
            return
        # Decode safely; truncate any trailing partial line
        text = chunk.decode("utf-8", errors="replace")
        lines = text.split("\n")
        # If chunk doesn't end with newline, the last element is a partial line.
        # Rewind the offset so the next read picks up the rest. JSONL line is
        # buffered until newline arrives — never emitted half-parsed.
        if chunk and not chunk.endswith(b"\n"):
            partial = lines.pop().encode("utf-8")
            with self._lock:
                self.offsets[key] = new_offset - len(partial)
            log.debug(
                "local tailer: %s ended mid-line (%d bytes buffered)",
                path.name, len(partial),
            )

        bucket = path.parent.name
        session_id = path.stem
        # Files under REMOTES_ROOT belong to a remote — namespace the bucket
        # so SSE events match the frontend's `remote:<host>:<bucket>` form.
        from .projects import REMOTES_ROOT  # local import to avoid cycle

        try:
            rel = path.relative_to(REMOTES_ROOT)
            # rel.parts[0] = host name, rel.parts[1] = original bucket
            if len(rel.parts) >= 2:
                host_name = rel.parts[0]
                bucket = f"remote:{host_name}:{bucket}"
        except ValueError:
            # Path isn't under REMOTES_ROOT — keep the local bucket name.
            pass

        for line in lines:
            ev = parser.parse_line(line)
            if not ev:
                continue
            self.broadcaster.publish(
                {
                    "kind": "event",
                    "bucket": bucket,
                    "session_id": session_id,
                    "event": ev.to_dict(),
                }
            )
        # Notify session list of mtime/size change
        self.broadcaster.publish(
            {
                "kind": "session-touched",
                "bucket": bucket,
                "session_id": session_id,
                "size": stat.st_size,
                "modified_ms": int(stat.st_mtime * 1000),
            }
        )


class Handler(FileSystemEventHandler):
    def __init__(self, tailer: JsonlTailer) -> None:
        self.tailer = tailer

    def _is_jsonl(self, p: str) -> bool:
        return p.endswith(".jsonl")

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory or not self._is_jsonl(event.src_path):
            return
        path = Path(event.src_path)
        # New file: full read
        self.tailer.force_full_read(path)
        self.tailer.consume_new(path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory or not self._is_jsonl(event.src_path):
            return
        self.tailer.consume_new(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if self._is_jsonl(event.dest_path):
            path = Path(event.dest_path)
            self.tailer.force_full_read(path)
            self.tailer.consume_new(path)


class WatcherService:
    def __init__(self) -> None:
        self.broadcaster = Broadcaster()
        self.tailer = JsonlTailer(self.broadcaster)
        self.observer: Observer | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.broadcaster.bind_loop(loop)
        # Initialize offsets for existing files (we serve history via REST,
        # so tailing only emits new content).
        roots = [r for r in (PROJECTS_ROOT, REMOTES_ROOT) if r.exists()]
        if not roots:
            return
        for root in roots:
            for jsonl in root.rglob("*.jsonl"):
                self.tailer.init_file(jsonl)
        self.observer = Observer()
        for root in roots:
            self.observer.schedule(
                Handler(self.tailer), str(root), recursive=True
            )
        self.observer.start()

    def stop(self) -> None:
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=2)
            self.observer = None
