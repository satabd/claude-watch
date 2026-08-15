"""SQLite cache for translations and scratchpad."""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import shutil
import sqlite3
import time
from pathlib import Path
from threading import RLock
from typing import Any, Callable

DB_PATH = Path.home() / ".claude" / "watcher" / "cache.sqlite"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_log = logging.getLogger("watcher.db")

# Reentrant: helper functions call other helpers (e.g. add_*  → get_*) and
# we don't want non-reentrant deadlocks when those nest while holding the lock.
_lock = RLock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
#
# We track schema version via SQLite's built-in ``PRAGMA user_version`` (an
# integer stored in the database header) instead of a separate metadata table.
# Each migration is a tuple ``(version, callable, description)`` that brings
# the schema from version ``N-1`` to ``N``. The callable receives the open
# connection and runs whatever statements it needs. The latest migration's
# version is the "current" schema.
#
# History encoded:
#
#   v1 - Original baseline: create translations, scratchpad, settings,
#        summaries, prompt_drafts, and remote_hosts (the latter without the
#        columns added later via the old ad-hoc ALTER TABLE pattern).
#   v2 - remote_hosts: add ``kind`` column.
#   v3 - remote_hosts: add ``status``, ``last_poll_ms``, ``last_event_ms``,
#        ``next_retry_ms`` columns (one ALTER each, since SQLite requires it).
#
# Existing users coming from the old code path may already have all the
# columns but a ``user_version`` of 0. Each ALTER-style migration uses
# ``try/except sqlite3.OperationalError`` internally so it is idempotent on
# already-migrated databases. The runner then bumps ``user_version`` to the
# latest version.

# v1 is a list of individual statements rather than one ``executescript``
# blob, because ``executescript`` issues an implicit COMMIT that breaks the
# outer transaction we wrap migrations in.
_MIGRATION_V1_STMTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS translations (
        source_hash  TEXT NOT NULL,
        target_lang  TEXT NOT NULL,
        source_text  TEXT NOT NULL,
        translation  TEXT NOT NULL,
        model        TEXT NOT NULL,
        created_at   INTEGER NOT NULL,
        PRIMARY KEY (source_hash, target_lang)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tr_created ON translations(created_at)",
    """CREATE TABLE IF NOT EXISTS scratchpad (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        action       TEXT NOT NULL,
        source_text  TEXT,
        source_turn  TEXT,
        result       TEXT NOT NULL,
        model        TEXT,
        session_id   TEXT,
        created_at   INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sp_created ON scratchpad(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_sp_session ON scratchpad(session_id)",
    """CREATE TABLE IF NOT EXISTS settings (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS summaries (
        content_hash  TEXT PRIMARY KEY,
        session_id    TEXT NOT NULL,
        summary       TEXT NOT NULL,
        model         TEXT NOT NULL,
        token_in      INTEGER,
        token_out     INTEGER,
        created_at    INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sum_session ON summaries(session_id)",
    """CREATE TABLE IF NOT EXISTS prompt_drafts (
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
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pd_session ON prompt_drafts(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_pd_created ON prompt_drafts(created_at)",
    """CREATE TABLE IF NOT EXISTS remote_hosts (
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
        created_at      INTEGER NOT NULL,
        updated_at      INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_rh_enabled ON remote_hosts(enabled)",
]


def _migration_v1(conn: sqlite3.Connection) -> None:
    for stmt in _MIGRATION_V1_STMTS:
        conn.execute(stmt)


def _safe_alter(conn: sqlite3.Connection, stmt: str) -> None:
    """Run an ALTER TABLE that's a no-op if the column already exists."""
    try:
        conn.execute(stmt)
    except sqlite3.OperationalError as exc:
        # SQLite raises OperationalError("duplicate column name: ...") when
        # the column is already there. Treat as already-applied.
        if "duplicate column name" in str(exc).lower():
            return
        raise


def _migration_v2(conn: sqlite3.Connection) -> None:
    _safe_alter(
        conn,
        "ALTER TABLE remote_hosts ADD COLUMN kind TEXT NOT NULL DEFAULT 'ssh'",
    )


def _migration_v3(conn: sqlite3.Connection) -> None:
    for stmt in (
        "ALTER TABLE remote_hosts ADD COLUMN status TEXT",
        "ALTER TABLE remote_hosts ADD COLUMN last_poll_ms INTEGER",
        "ALTER TABLE remote_hosts ADD COLUMN last_event_ms INTEGER",
        "ALTER TABLE remote_hosts ADD COLUMN next_retry_ms INTEGER",
    ):
        _safe_alter(conn, stmt)


# v4 — Review Threads. Two new tables for the reviewer-pair workflow: a
# user discusses a Claude Code result with another LLM (Codex/Gemini/...) to
# critique it and write the next prompt. Threads are forward-focused — old
# messages stay in the DB for display/search/audit but are not auto-replayed
# to the reviewer.
_MIGRATION_V4_STMTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS review_threads (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        name                 TEXT NOT NULL,
        provider             TEXT NOT NULL,
        project_bucket       TEXT,
        claude_session_id    TEXT,
        provider_session_id  TEXT,
        created_at           INTEGER NOT NULL,
        updated_at           INTEGER NOT NULL,
        archived_at          INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_rt_bucket ON review_threads(project_bucket, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_rt_active ON review_threads(archived_at, updated_at DESC)",
    """CREATE TABLE IF NOT EXISTS review_messages (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id            INTEGER NOT NULL REFERENCES review_threads(id) ON DELETE CASCADE,
        role                 TEXT NOT NULL,
        content              TEXT NOT NULL,
        source_session_id    TEXT,
        source_turn_uuid     TEXT,
        context_used_json    TEXT,
        evidence_used_json   TEXT,
        provider             TEXT,
        model                TEXT,
        estimated_tokens     INTEGER,
        provider_tokens      INTEGER,
        created_at           INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_rm_thread ON review_messages(thread_id, created_at)",
]


def _migration_v4(conn: sqlite3.Connection) -> None:
    for stmt in _MIGRATION_V4_STMTS:
        conn.execute(stmt)


# v5 — Review Skills versioning. Adds three columns to review_threads so
# we can track WHICH skill is active and which (skill_id, skill_version)
# pair the stored Codex provider session was created against. The send
# route refuses to resume a session whose stored pair doesn't match the
# current selection, guaranteeing a clean break when skill instructions
# change meaningfully.
def _migration_v5(conn: sqlite3.Connection) -> None:
    for stmt in (
        "ALTER TABLE review_threads ADD COLUMN active_skill_id TEXT",
        "ALTER TABLE review_threads ADD COLUMN provider_session_skill_id TEXT",
        "ALTER TABLE review_threads ADD COLUMN provider_session_skill_version INTEGER",
    ):
        _safe_alter(conn, stmt)


# v6 — Zellij runtime control. `runtime_bindings` maps a Claude session id
# (permanent identity) to its current disposable Zellij runtime; rows are
# verified against `zellij list-sessions` on every use and deleted when
# stale. `pending_prompts` holds composer drafts server-side so they survive
# reloads and so the send transition can be made atomic (the UPDATE ...
# WHERE status='pending' rowcount check is the double-send guard).
def _migration_v6(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runtime_bindings (
            claude_session_id TEXT PRIMARY KEY,
            zellij_session TEXT NOT NULL,
            pane_id TEXT NOT NULL,
            cwd TEXT,
            created_ms INTEGER NOT NULL,
            last_verified_ms INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pending_prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket TEXT NOT NULL,
            session_id TEXT NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_ms INTEGER NOT NULL,
            updated_ms INTEGER NOT NULL,
            sent_ms INTEGER
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_prompts_session "
        "ON pending_prompts (bucket, session_id, status)"
    )


# v7 — remember which Zellij *tab* holds a session's pane. Zellij can report
# tab names and pane ids, but nothing maps one to the other
# (`dump-layout` lists panes without their ids), so the only reliable record
# is the name we chose when we created the tab.
def _migration_v7(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runtime_bindings)")}
    if "tab_name" not in cols:
        conn.execute("ALTER TABLE runtime_bindings ADD COLUMN tab_name TEXT")


# v8 — record the pid we spawned. Ownership used to be re-derived on every
# request from pane titles and process scans, which is guessing: a title is a
# string anyone can produce, and a surviving pane says nothing about whether
# the claude inside it is still alive. Storing the pid makes "managed" a fact
# we wrote down — this session is ours iff THIS pid is still running and still
# a claude — and it bounds teardown to the one process we started.
def _migration_v8(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runtime_bindings)")}
    if "pid" not in cols:
        conn.execute("ALTER TABLE runtime_bindings ADD COLUMN pid INTEGER")


# Ordered list of (version, sql_or_callable, description). The last entry's
# version number IS the current schema version.
MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None], str]] = [
    (1, _migration_v1, "initial schema"),
    (2, _migration_v2, "remote_hosts: add kind"),
    (
        3,
        _migration_v3,
        "remote_hosts: add status, last_poll_ms, last_event_ms, next_retry_ms",
    ),
    (4, _migration_v4, "review_threads + review_messages"),
    (
        5,
        _migration_v5,
        "review_threads: add active_skill_id, provider_session_skill_id, "
        "provider_session_skill_version",
    ),
    (6, _migration_v6, "runtime_bindings + pending_prompts (Zellij control)"),
    (7, _migration_v7, "runtime_bindings: add tab_name"),
    (8, _migration_v8, "runtime_bindings: add pid (recorded ownership)"),
]


def _latest_version() -> int:
    return MIGRATIONS[-1][0] if MIGRATIONS else 0


def schema_version() -> int:
    """Return the current ``PRAGMA user_version`` of the DB."""
    with _lock, _conn() as c:
        row = c.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row is not None else 0


def _backup_db(target_version: int) -> Path | None:
    """Copy the live DB next to itself with a versioned suffix. No-op if the
    DB file does not exist yet (fresh install)."""
    if not DB_PATH.exists():
        return None
    iso = _dt.datetime.now().replace(microsecond=0).isoformat().replace(":", "-")
    backup = DB_PATH.with_name(
        f"{DB_PATH.name}.backup-pre-v{target_version}-{iso}"
    )
    shutil.copy2(DB_PATH, backup)
    return backup


def init() -> None:
    """Create the DB if needed and run any pending migrations.

    On a fresh install we create the file, leave ``user_version`` at 0, then
    run all migrations sequentially in a single transaction. On an existing
    DB we run only the migrations whose version is greater than the current
    ``user_version`` -- after taking a backup. If any migration raises, the
    transaction rolls back and the on-disk DB is unchanged; the
    ``.backup-pre-vN`` file remains as a safety net for catastrophic cases.
    """
    latest = _latest_version()
    fresh = not DB_PATH.exists()

    with _lock:
        if fresh:
            # Create an empty file so subsequent connections share the same
            # path. user_version defaults to 0 on a brand-new DB.
            DB_PATH.touch()
            _log.info("fresh DB at %s, running migrations 1..%d", DB_PATH, latest)

        # Use isolation_level=None for manual transaction control. Python's
        # sqlite3 driver otherwise auto-commits DDL and silently messes with
        # explicit BEGIN/COMMIT, which we need to wrap the whole batch.
        c = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        try:
            current = int(c.execute("PRAGMA user_version").fetchone()[0])

            if current >= latest:
                _log.info("schema is up to date (version %d)", current)
                return

            pending = [m for m in MIGRATIONS if m[0] > current]
            if not pending:
                # Defensive: shouldn't happen given the check above.
                _log.info("schema is up to date (version %d)", current)
                return

            if not fresh:
                backup = _backup_db(pending[0][0])
                if backup is not None:
                    _log.info("backed up DB to %s", backup)

            _log.info(
                "applying %d migration(s): %s -> %d",
                len(pending),
                current,
                latest,
            )

            try:
                c.execute("BEGIN")
                for version, body, desc in pending:
                    _log.info("migration v%d: %s", version, desc)
                    body(c)
                # PRAGMA user_version doesn't accept parameter binding.
                c.execute(f"PRAGMA user_version = {latest}")
                c.execute("COMMIT")
            except Exception:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                _log.exception("migration failed; rolled back")
                raise

            _log.info("migrations complete; schema now at version %d", latest)
        finally:
            c.close()


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


# ---------------------------------------------------------------------------
# Review Threads (v4)
# ---------------------------------------------------------------------------


# Sentinel: distinguishes "leave column alone" from "set column to NULL" in
# patch helpers. Caller passes ``None`` to clear a column or omits the kwarg
# entirely (defaulted to _UNSET) to skip it.
class _Sentinel:
    pass


_UNSET = _Sentinel()


def create_review_thread(
    *,
    name: str,
    provider: str,
    project_bucket: str | None,
    claude_session_id: str | None,
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    with _lock, _conn() as c:
        cur = c.execute(
            """INSERT INTO review_threads
               (name, provider, project_bucket, claude_session_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, provider, project_bucket, claude_session_id, now, now),
        )
        thread_id = cur.lastrowid
    return get_review_thread(thread_id)


def get_review_thread(thread_id: int) -> dict[str, Any] | None:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM review_threads WHERE id = ?", (thread_id,)
        ).fetchone()
    return dict(row) if row else None


def list_review_threads(
    *, project_bucket: str | None = None, include_archived: bool = False
) -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        if project_bucket and not include_archived:
            rows = c.execute(
                "SELECT * FROM review_threads WHERE archived_at IS NULL AND project_bucket = ? "
                "ORDER BY updated_at DESC",
                (project_bucket,),
            ).fetchall()
        elif project_bucket:
            rows = c.execute(
                "SELECT * FROM review_threads WHERE project_bucket = ? "
                "ORDER BY updated_at DESC",
                (project_bucket,),
            ).fetchall()
        elif not include_archived:
            rows = c.execute(
                "SELECT * FROM review_threads WHERE archived_at IS NULL "
                "ORDER BY updated_at DESC"
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM review_threads ORDER BY updated_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def update_review_thread(
    thread_id: int,
    *,
    name: str | None = None,
    provider_session_id: str | None | _Sentinel = _UNSET,
    active_skill_id: str | None | _Sentinel = _UNSET,
    provider_session_skill_id: str | None | _Sentinel = _UNSET,
    provider_session_skill_version: int | None | _Sentinel = _UNSET,
    archived: bool | None = None,
) -> dict[str, Any] | None:
    """Patch a thread. ``_UNSET`` (the default for sentinel-typed fields)
    means "leave column alone". Pass ``None`` explicitly to clear a
    column; pass a value to replace it.

    Skill-related fields exist for the Review Skills versioning scheme:
    the send route stores the (skill_id, version) pair under which the
    Codex session was created, then refuses to resume the session if the
    user later selects a different skill or the version is bumped."""
    fields: list[str] = []
    params: list[Any] = []
    if name is not None:
        fields.append("name = ?")
        params.append(name)
    if provider_session_id is not _UNSET:
        fields.append("provider_session_id = ?")
        params.append(provider_session_id)
    if active_skill_id is not _UNSET:
        fields.append("active_skill_id = ?")
        params.append(active_skill_id)
    if provider_session_skill_id is not _UNSET:
        fields.append("provider_session_skill_id = ?")
        params.append(provider_session_skill_id)
    if provider_session_skill_version is not _UNSET:
        fields.append("provider_session_skill_version = ?")
        params.append(provider_session_skill_version)
    if archived is True:
        fields.append("archived_at = ?")
        params.append(int(time.time() * 1000))
    elif archived is False:
        fields.append("archived_at = NULL")
    if not fields:
        return get_review_thread(thread_id)
    fields.append("updated_at = ?")
    params.append(int(time.time() * 1000))
    params.append(thread_id)
    with _lock, _conn() as c:
        c.execute(
            f"UPDATE review_threads SET {', '.join(fields)} WHERE id = ?", params
        )
    return get_review_thread(thread_id)


def add_review_message(
    *,
    thread_id: int,
    role: str,
    content: str,
    source_session_id: str | None = None,
    source_turn_uuid: str | None = None,
    context_used_json: str | None = None,
    evidence_used_json: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    estimated_tokens: int | None = None,
    provider_tokens: int | None = None,
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    with _lock, _conn() as c:
        cur = c.execute(
            """INSERT INTO review_messages
               (thread_id, role, content, source_session_id, source_turn_uuid,
                context_used_json, evidence_used_json, provider, model,
                estimated_tokens, provider_tokens, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                thread_id,
                role,
                content,
                source_session_id,
                source_turn_uuid,
                context_used_json,
                evidence_used_json,
                provider,
                model,
                estimated_tokens,
                provider_tokens,
                now,
            ),
        )
        msg_id = cur.lastrowid
    return get_review_message(msg_id)


def get_review_message(message_id: int) -> dict[str, Any] | None:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM review_messages WHERE id = ?", (message_id,)
        ).fetchone()
    return dict(row) if row else None


def list_review_messages(thread_id: int) -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM review_messages WHERE thread_id = ? ORDER BY created_at ASC",
            (thread_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Zellij runtime control (bindings + pending prompts)
# ---------------------------------------------------------------------------

def runtime_binding_get(claude_session_id: str) -> dict[str, Any] | None:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM runtime_bindings WHERE claude_session_id = ?",
            (claude_session_id,),
        ).fetchone()
    return dict(row) if row else None


def runtime_binding_put(
    claude_session_id: str,
    zellij_session: str,
    pane_id: str,
    cwd: str | None,
    tab_name: str | None = None,
    pid: int | None = None,
) -> None:
    now = int(time.time() * 1000)
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO runtime_bindings "
            "(claude_session_id, zellij_session, pane_id, cwd, tab_name, pid, "
            " created_ms, last_verified_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claude_session_id, zellij_session, pane_id, cwd, tab_name, pid,
                now, now,
            ),
        )


def runtime_binding_touch(claude_session_id: str) -> None:
    with _lock, _conn() as c:
        c.execute(
            "UPDATE runtime_bindings SET last_verified_ms = ? "
            "WHERE claude_session_id = ?",
            (int(time.time() * 1000), claude_session_id),
        )


def runtime_binding_delete(claude_session_id: str) -> None:
    with _lock, _conn() as c:
        c.execute(
            "DELETE FROM runtime_bindings WHERE claude_session_id = ?",
            (claude_session_id,),
        )


def pending_prompt_add(bucket: str, session_id: str, text: str) -> dict[str, Any]:
    now = int(time.time() * 1000)
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO pending_prompts "
            "(bucket, session_id, text, status, created_ms, updated_ms) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (bucket, session_id, text, now, now),
        )
        pid = cur.lastrowid
    return pending_prompt_get(pid)  # type: ignore[return-value]


def pending_prompt_get(prompt_id: int) -> dict[str, Any] | None:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM pending_prompts WHERE id = ?", (prompt_id,)
        ).fetchone()
    return dict(row) if row else None


def pending_prompt_list(bucket: str, session_id: str) -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM pending_prompts "
            "WHERE bucket = ? AND session_id = ? AND status = 'pending' "
            "ORDER BY created_ms ASC",
            (bucket, session_id),
        ).fetchall()
    return [dict(r) for r in rows]


def pending_prompt_update_text(prompt_id: int, text: str) -> bool:
    """Edit a draft. Only pending prompts are editable."""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE pending_prompts SET text = ?, updated_ms = ? "
            "WHERE id = ? AND status = 'pending'",
            (text, int(time.time() * 1000), prompt_id),
        )
        return cur.rowcount > 0


def pending_prompt_delete(prompt_id: int) -> bool:
    with _lock, _conn() as c:
        cur = c.execute(
            "DELETE FROM pending_prompts WHERE id = ? AND status = 'pending'",
            (prompt_id,),
        )
        return cur.rowcount > 0


def pending_prompt_claim(prompt_id: int) -> bool:
    """Atomically move pending -> sending. False means someone else already
    claimed/sent it — the double-send guard for UI retries."""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE pending_prompts SET status = 'sending', updated_ms = ? "
            "WHERE id = ? AND status = 'pending'",
            (int(time.time() * 1000), prompt_id),
        )
        return cur.rowcount > 0


def pending_prompt_finish(prompt_id: int, ok: bool) -> None:
    """sending -> sent on success, sending -> pending on failure (retryable)."""
    now = int(time.time() * 1000)
    with _lock, _conn() as c:
        if ok:
            c.execute(
                "UPDATE pending_prompts SET status = 'sent', sent_ms = ?, "
                "updated_ms = ? WHERE id = ? AND status = 'sending'",
                (now, now, prompt_id),
            )
        else:
            c.execute(
                "UPDATE pending_prompts SET status = 'pending', updated_ms = ? "
                "WHERE id = ? AND status = 'sending'",
                (now, prompt_id),
            )
