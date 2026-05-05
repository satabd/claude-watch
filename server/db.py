"""SQLite cache for translations and scratchpad."""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from threading import RLock
from typing import Any

DB_PATH = Path.home() / ".claude" / "watcher" / "cache.sqlite"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Reentrant: helper functions call other helpers (e.g. add_*  → get_*) and
# we don't want non-reentrant deadlocks when those nest while holding the lock.
_lock = RLock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS translations (
                source_hash  TEXT NOT NULL,
                target_lang  TEXT NOT NULL,
                source_text  TEXT NOT NULL,
                translation  TEXT NOT NULL,
                model        TEXT NOT NULL,
                created_at   INTEGER NOT NULL,
                PRIMARY KEY (source_hash, target_lang)
            );
            CREATE INDEX IF NOT EXISTS idx_tr_created ON translations(created_at);

            CREATE TABLE IF NOT EXISTS scratchpad (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                action       TEXT NOT NULL,
                source_text  TEXT,
                source_turn  TEXT,
                result       TEXT NOT NULL,
                model        TEXT,
                session_id   TEXT,
                created_at   INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sp_created ON scratchpad(created_at);
            CREATE INDEX IF NOT EXISTS idx_sp_session ON scratchpad(session_id);

            CREATE TABLE IF NOT EXISTS settings (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS summaries (
                content_hash  TEXT PRIMARY KEY,
                session_id    TEXT NOT NULL,
                summary       TEXT NOT NULL,
                model         TEXT NOT NULL,
                token_in      INTEGER,
                token_out     INTEGER,
                created_at    INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sum_session ON summaries(session_id);

            CREATE TABLE IF NOT EXISTS prompt_drafts (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket             TEXT NOT NULL,
                session_id         TEXT NOT NULL,
                source_event_uuid  TEXT,
                mode               TEXT NOT NULL,
                context_mode       TEXT NOT NULL,
                rough_input        TEXT NOT NULL,
                generated_prompt   TEXT NOT NULL,
                improvement_notes  TEXT,
                context_used       TEXT,
                context_chars      INTEGER,
                model              TEXT,
                created_at         INTEGER NOT NULL,
                updated_at         INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pd_session ON prompt_drafts(session_id);
            CREATE INDEX IF NOT EXISTS idx_pd_created ON prompt_drafts(created_at);

            CREATE TABLE IF NOT EXISTS remote_hosts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL UNIQUE,
                host            TEXT NOT NULL,
                port            INTEGER NOT NULL DEFAULT 22,
                username        TEXT NOT NULL,
                key_path        TEXT,
                projects_path   TEXT,
                home_dir        TEXT,
                platform        TEXT,
                enabled         INTEGER NOT NULL DEFAULT 1,
                last_synced_ms  INTEGER,
                last_error      TEXT,
                kind            TEXT NOT NULL DEFAULT 'ssh',
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rh_enabled ON remote_hosts(enabled);
            """
        )
        # Light migrations for newer columns (no-op if they exist)
        for stmt in (
            "ALTER TABLE remote_hosts ADD COLUMN kind TEXT NOT NULL DEFAULT 'ssh'",
            "ALTER TABLE remote_hosts ADD COLUMN status TEXT",
            "ALTER TABLE remote_hosts ADD COLUMN last_poll_ms INTEGER",
            "ALTER TABLE remote_hosts ADD COLUMN last_event_ms INTEGER",
            "ALTER TABLE remote_hosts ADD COLUMN next_retry_ms INTEGER",
        ):
            try:
                c.execute(stmt)
            except sqlite3.OperationalError:
                pass


def get_summary(content_hash: str) -> dict[str, Any] | None:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM summaries WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return dict(row) if row else None


def save_summary(
    content_hash: str,
    session_id: str,
    summary: str,
    model: str,
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    with _lock, _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO summaries
               (content_hash, session_id, summary, model, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (content_hash, session_id, summary, model, now),
        )
    return {
        "content_hash": content_hash,
        "session_id": session_id,
        "summary": summary,
        "model": model,
        "created_at": now,
    }


def add_prompt_draft(
    *,
    bucket: str,
    session_id: str,
    source_event_uuid: str | None,
    mode: str,
    context_mode: str,
    rough_input: str,
    generated_prompt: str,
    improvement_notes: str | None,
    context_used: str | None,
    context_chars: int | None,
    model: str | None,
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    with _lock, _conn() as c:
        cur = c.execute(
            """INSERT INTO prompt_drafts
               (bucket, session_id, source_event_uuid, mode, context_mode,
                rough_input, generated_prompt, improvement_notes,
                context_used, context_chars, model, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                bucket,
                session_id,
                source_event_uuid,
                mode,
                context_mode,
                rough_input,
                generated_prompt,
                improvement_notes,
                context_used,
                context_chars,
                model,
                now,
                now,
            ),
        )
        draft_id = cur.lastrowid
    return get_prompt_draft(draft_id)


def update_prompt_draft(
    draft_id: int,
    *,
    generated_prompt: str | None = None,
    improvement_notes: str | None = None,
) -> dict[str, Any] | None:
    now = int(time.time() * 1000)
    sets: list[str] = []
    args: list[Any] = []
    if generated_prompt is not None:
        sets.append("generated_prompt = ?")
        args.append(generated_prompt)
    if improvement_notes is not None:
        sets.append("improvement_notes = ?")
        args.append(improvement_notes)
    if not sets:
        return get_prompt_draft(draft_id)
    sets.append("updated_at = ?")
    args.append(now)
    args.append(draft_id)
    with _lock, _conn() as c:
        c.execute(
            f"UPDATE prompt_drafts SET {', '.join(sets)} WHERE id = ?",
            tuple(args),
        )
    return get_prompt_draft(draft_id)


def get_prompt_draft(draft_id: int) -> dict[str, Any] | None:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM prompt_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        return dict(row) if row else None


def list_prompt_drafts(
    session_id: str | None = None, limit: int = 30
) -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        if session_id:
            rows = c.execute(
                "SELECT * FROM prompt_drafts WHERE session_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM prompt_drafts ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def delete_prompt_draft(draft_id: int) -> bool:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM prompt_drafts WHERE id = ?", (draft_id,))
        return cur.rowcount > 0


# ---- Remote hosts ----


def list_remote_hosts() -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM remote_hosts ORDER BY name ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_remote_host(host_id: int) -> dict[str, Any] | None:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM remote_hosts WHERE id = ?", (host_id,)
        ).fetchone()
        return dict(row) if row else None


def get_remote_host_by_name(name: str) -> dict[str, Any] | None:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM remote_hosts WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None


def add_remote_host(
    *,
    name: str,
    host: str,
    port: int,
    username: str,
    key_path: str | None,
    projects_path: str | None,
    kind: str = "ssh",
    home_dir: str | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    with _lock, _conn() as c:
        cur = c.execute(
            """INSERT INTO remote_hosts
               (name, host, port, username, key_path, projects_path,
                home_dir, platform, kind, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                name,
                host,
                port,
                username,
                key_path,
                projects_path,
                home_dir,
                platform,
                kind,
                now,
                now,
            ),
        )
        new_id = cur.lastrowid
    return get_remote_host(new_id) or {}


def update_remote_host(host_id: int, **fields: Any) -> dict[str, Any] | None:
    if not fields:
        return get_remote_host(host_id)
    cols: list[str] = []
    args: list[Any] = []
    allowed = {
        "name",
        "host",
        "port",
        "username",
        "key_path",
        "projects_path",
        "home_dir",
        "platform",
        "enabled",
        "last_synced_ms",
        "last_error",
        "status",
        "last_poll_ms",
        "last_event_ms",
        "next_retry_ms",
    }
    for k, v in fields.items():
        if k not in allowed:
            continue
        cols.append(f"{k} = ?")
        args.append(v)
    if not cols:
        return get_remote_host(host_id)
    cols.append("updated_at = ?")
    args.append(int(time.time() * 1000))
    args.append(host_id)
    with _lock, _conn() as c:
        c.execute(
            f"UPDATE remote_hosts SET {', '.join(cols)} WHERE id = ?", tuple(args)
        )
    return get_remote_host(host_id)


def delete_remote_host(host_id: int) -> bool:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM remote_hosts WHERE id = ?", (host_id,))
        return cur.rowcount > 0


def get_setting(key: str, default: str | None = None) -> str | None:
    with _lock, _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def all_settings() -> dict[str, str]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_translation(source_text: str, target_lang: str) -> dict[str, Any] | None:
    h = hash_text(source_text)
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM translations WHERE source_hash = ? AND target_lang = ?",
            (h, target_lang),
        ).fetchone()
        return dict(row) if row else None


def get_translations_for_hashes(
    hashes: list[str], target_lang: str
) -> dict[str, dict[str, Any]]:
    """Bulk-lookup translations by source-hash list. Returns hash -> row dict."""
    if not hashes:
        return {}
    out: dict[str, dict[str, Any]] = {}
    # Batch in groups to keep the SQL placeholder count under SQLite's limit
    BATCH = 500
    with _lock, _conn() as c:
        for i in range(0, len(hashes), BATCH):
            chunk = hashes[i : i + BATCH]
            placeholders = ",".join("?" * len(chunk))
            rows = c.execute(
                f"SELECT * FROM translations "
                f"WHERE target_lang = ? AND source_hash IN ({placeholders})",
                [target_lang, *chunk],
            ).fetchall()
            for r in rows:
                out[r["source_hash"]] = dict(r)
    return out


def save_translation(
    source_text: str, target_lang: str, translation: str, model: str
) -> dict[str, Any]:
    h = hash_text(source_text)
    now = int(time.time() * 1000)
    with _lock, _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO translations
               (source_hash, target_lang, source_text, translation, model, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (h, target_lang, source_text, translation, model, now),
        )
    return {
        "source_hash": h,
        "target_lang": target_lang,
        "source_text": source_text,
        "translation": translation,
        "model": model,
        "created_at": now,
    }


def add_scratchpad(
    action: str,
    result: str,
    *,
    source_text: str | None = None,
    source_turn: str | None = None,
    model: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    with _lock, _conn() as c:
        cur = c.execute(
            """INSERT INTO scratchpad
               (action, source_text, source_turn, result, model, session_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (action, source_text, source_turn, result, model, session_id, now),
        )
        sp_id = cur.lastrowid
    return {
        "id": sp_id,
        "action": action,
        "source_text": source_text,
        "source_turn": source_turn,
        "result": result,
        "model": model,
        "session_id": session_id,
        "created_at": now,
    }


def list_scratchpad(session_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        if session_id:
            rows = c.execute(
                "SELECT * FROM scratchpad WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM scratchpad ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def delete_scratchpad(item_id: int) -> bool:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM scratchpad WHERE id = ?", (item_id,))
        return cur.rowcount > 0
