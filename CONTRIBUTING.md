# Contributing

A short guide for working on Claude Watcher — conventions, the test rule,
and the architecture knowledge you'd otherwise have to reverse-engineer.

---

## Dev workflow

```bash
# Backend (live reload via uvicorn --reload, optional)
.venv/Scripts/python -m uvicorn server.main:app --port 8765 --reload

# Frontend (Vite dev server, proxies /api and /sse to :8765)
cd web && npm run dev
```

Open `http://localhost:5174`. The Vite dev server has its own port and
forwards API calls to the Python backend.

For a "production-like" check before committing:

```bash
cd web && npm run build
.venv/Scripts/python -m uvicorn server.main:app --port 8765
# Open http://localhost:8765 (FastAPI serves the built UI)
```

---

## The test rule

**No test ever writes to a real Claude session JSONL.** This rule was added
after an early reliability run left 15 lines of test markers in two real
session files (`reliability-test`, `watcher-test`, etc.). The lines were
harmless — Claude Code's parser ignores unknown event types — but they
shouldn't have been there.

If you write a new test that needs to mutate a JSONL:

1. **Create a sandbox bucket** under the user's WSL `~/.claude/projects/`
   with a unique, easily-identifiable name. The convention is
   `_watcher-test-<isoseconds-utc>`. The leading underscore sorts the bucket
   last in the sidebar tree and signals "this is test scaffolding."
2. **Seed it with one valid JSONL line** so the watcher's full-scan picks
   it up as a real bucket.
3. **Write a `trap cleanup EXIT`** that removes the bucket from the remote
   and the local mirror — even on Ctrl-C or test failure.

Reference implementation: `test_reliability.sh` (sandbox setup at the top,
`cleanup()` + `trap` immediately after, all 7 phases use only the sandbox
file).

Run with:

```bash
bash test_reliability.sh
```

Expected output: `7/7 PASS`, then `(clean — no test buckets remain)`.

---

## Architecture quick-reference

For the full picture see README.md → "Architecture (for developers)". This
file covers the parts that bit me during development and that the next
contributor would otherwise rediscover.

### How sessions actually flow across machines

A common confusion: "Claude Desktop and Claude CLI don't share sessions" is
**partially false**. The full picture, validated by on-disk evidence:

1. `claude` CLI on host X writes its session JSONL to `~/.claude/projects/`
   on host X. **This is the canonical copy.**
2. When **Claude Desktop** uses its SSH/remote feature to attach to a
   session running on host X, Desktop **caches** that session's JSONL on
   the *Desktop client's* local disk under
   `~/.claude/projects/ssh-<session_id>/<session_id>.jsonl`.
   The bucket name is the **session UUID**, not a connection ID — one
   bucket per session attached.
3. Both files have the same content from byte 0 (modulo whatever streamed
   in before Desktop attached) and the same `session_id`.
4. So a single conversation can have **two physical files on two
   filesystems** with the same UUID.

The watcher merges them by `session_id` (`dedupeSessions` in `project-tree.ts`)
and prefers the larger / more recent file — usually the one that's still
being appended to live.

### `bucket` naming conventions

The `bucket` field on a session can take three forms:

| Form | Where | Example |
|---|---|---|
| `<encoded-cwd>` | Local CLI session | `D--VibeProjects-Foo` |
| `ssh-<uuid>` | Claude Desktop's local cache of an SSH-attached session | `ssh-c78a73db-…` |
| `remote:<host>:<bucket>` | Watcher's mirror of a remote session via SFTP | `remote:wsl-ubuntu:-home-sat-vibeprojects-foo` |

The frontend always uses these strings as keys. `find_session(bucket, id)`
in `projects.py` understands all three.

### SSE event shape

Published by the byte-offset tailer in `watcher.py`:

```jsonc
// kind: "event" — one parsed JSONL row
{
  "kind": "event",
  "bucket": "remote:wsl-ubuntu:-home-sat-vibeprojects-foo",
  "session_id": "abc12345-...",
  "event": { "type": "assistant", "uuid": "...", ... }
}

// kind: "session-touched" — file mtime/size changed; sidebar can refresh stats
{
  "kind": "session-touched",
  "bucket": "...",
  "session_id": "...",
  "size": 14786,
  "modified_ms": 1234567890
}

// kind: "hello" — sent once when the SSE stream connects
{ "kind": "hello", "ok": true }

// kind: "ping" — every 15s heartbeat
{ "kind": "ping" }
```

### Provider subprocess gotchas

If you add a new provider:

- **Don't use `$(...)` substitution in scripts you pass to `wsl -- sh -c`.**
  Windows argv quoting layers occasionally re-evaluate it before the WSL
  shell sees it (we got `sh: 1: configuration: Permission denied` because
  grep output got interpreted as a command name).
- **Don't use `\1` backreferences** inside Python source strings that flow
  to a remote shell. `"\\1"` parses cleanly in Python (2 chars), but the
  layered escaping when bash heredocs / Git Bash arg-mangling get involved
  means you'll see byte 0x01 land on the remote.
- **Prefer `127.0.0.1` over `localhost`** when constructing connection
  hostnames. asyncssh resolves `localhost` to IPv6 `::1` first, and
  WSL2's localhost forwarding is IPv4-only.

These three were each a separate hour of debugging. The fixes are encoded
in `server/wsl.py` and `server/remotes.py`; don't undo them.

### Database migrations

Schema changes are additive: add a `CREATE TABLE IF NOT EXISTS …` block to
`db.init()`, and an `ALTER TABLE … ADD COLUMN …` for new columns on existing
tables. Wrap the ALTER in `try/except sqlite3.OperationalError` so re-runs
on a DB that already has the column don't crash.

Don't drop or rename columns. The DB lives at
`~/.claude/watcher/cache.sqlite` and is the user's only state — losing it
loses every translation, scratchpad note, and prompt draft.

### Asyncio + RLock

`db._lock` is a `threading.RLock`, not a `Lock`. Several DB helpers
internally call other DB helpers (e.g. `add_remote_host` followed by
`get_remote_host` in two separate `with _lock` blocks). Once upon a time
this was a non-reentrant `Lock` and it deadlocked the whole server —
every DB-using endpoint hung. RLock is the right choice; please don't
"simplify" it back.

### Routes that mutate state must be `async def`

If a sync FastAPI handler tries `asyncio.create_task(...)`, it raises
`RuntimeError("no running event loop")` because sync handlers run on a
threadpool thread. We hit this when remote host CRUD silently failed to
trigger `manager.reload()`. Make the handler `async def` and `await` the
manager call directly.

---

## Code style

- **Python 3.10+** features OK (`X | Y`, `match`, structural-pattern unions).
- **Type-annotate** at module boundaries; the body of a 5-line helper can
  skip them.
- **Tolerant parsing**: anything reading the JSONL must treat unknown event
  types and missing fields as "ignore and continue", not "raise".
- **No `print()`** in `server/`; use `logging.getLogger("watcher.…").info/…`.
- **Frontend** uses Tailwind utility classes; no per-component CSS files.
  Reach for `cn()` helper for conditional classes. shadcn primitives in
  `components/ui/` are owned by us — copy/adapt as needed; don't fork the
  upstream library.
- **No comments narrating obvious code.** Comments earn their place by
  explaining *why*, especially around the gotchas above.

---

## Things to avoid

- **Touching real session JSONLs from tests.** See "The test rule" above.
- **Adding API keys / secrets.** This tool deliberately uses subprocess CLI
  auth (claude, codex). The day we accept an API key is the day we need
  proper secret storage, which we don't have.
- **Auto-cleaning the user's data.** Remote-mirror dirs and SQLite caches
  are intentionally append-forever; the user removes them manually if they
  want to.
- **Polling more aggressively than `WATCHER_REMOTE_ACTIVE_POLL_S`.** If a
  feature needs sub-2s freshness, drive it from SSE events the watcher is
  already publishing rather than adding a new polling loop.

---

## When in doubt

Read the existing tests (`test_reliability.sh`) — they exercise the most
fragile boundaries (host lifecycle, partial-line parsing, SSE namespacing,
SSH failure recovery) and capture the expected behavior in checkable form.
