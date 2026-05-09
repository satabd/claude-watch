# claude-watcher / ContextDesk — Project Reference

**Snapshot:** HEAD `be7c254` · DB schema **v5** · Review Skills V1 ·
generated 2026-05-09.

This document is a *consolidation* artifact. It describes what the
codebase does **today**, with no aspirational language. Where something
is inferred rather than directly verified in code, it is marked
**(inferred)**. Nothing here adds new features.

---

## Table of contents

1. [Current Project Overview](#1-current-project-overview)
2. [DB Schema](#2-db-schema)
3. [Migration History](#3-migration-history)
4. [Review Threads Workflow](#4-review-threads-workflow)
5. [Prompt Writer Workflow](#5-prompt-writer-workflow)
6. [Remote Watcher Workflow](#6-remote-watcher-workflow)
7. [Main UI Map](#7-main-ui-map)
8. [Glossary](#8-glossary)
9. [Known Limitations](#9-known-limitations)
10. [Developer Commands](#10-developer-commands)
11. [Recommended Next Steps](#11-recommended-next-steps)

---

## 1. Current Project Overview

### What it is

A standalone, **local-only** viewer for Claude Code session files. Lives
on your machine, reads `~/.claude/projects/*.jsonl`, and renders a
modern shadcn/ui interface around them. Backend is FastAPI; frontend is
Vite + React 18 + TypeScript + Tailwind + Zustand.

### Naming

The git repo and Python package are named **`claude-watcher`**. The
specs in recent feature work have referred to the same product as
**`ContextDesk`**. There is no rename in code yet — file paths,
processes, log channels, and SQLite filenames all still say
`claude-watcher` / `watcher.*`. **Treat the two names as
interchangeable** until a real rename lands.

### What problem it solves

Claude Code (CLI and Desktop) writes every session as a JSONL file
under `~/.claude/projects/<bucket>/<session_id>.jsonl`. There's no
official UI for browsing, searching, translating, summarizing, or
reviewing those sessions side-by-side. Specifically:

- You can't easily see a remote / WSL Claude session from your laptop's
  Claude Desktop sidebar.
- You can't do a follow-up read-only operation (translate, summarize,
  ask a question about the work) without polluting the live session
  the agent is in.
- You can't easily get a *second opinion* on what Claude Code just
  produced.

claude-watcher addresses these by mirroring sessions in real time
(local watchdog + SSH/SFTP polling), exposing them in a chat-like UI,
and adding three side-channel features that operate without modifying
the source session: **Translate / Scratchpad actions**, the
**Prompt Writer**, and **Review Threads**.

### Main user workflow

1. Run the backend (`uvicorn server.main:app …`) and the built UI is
   served at the same port.
2. The sidebar lists every project bucket and its sessions.
3. Click a session → the timeline renders the parsed turns.
4. Optional: per-turn translate to Arabic; selection-toolbar actions
   for clarify/explain/summarize; per-assistant action row for
   `[Review this] [Write prompt from this] [Copy]`.
5. Use the **Review Panel** to discuss a Claude assistant message
   with another LLM (Codex), get a verdict, get a copy-ready next
   prompt to paste back to Claude Code.
6. Use the **Prompt Writer** to draft / refine a prompt with a
   chosen amount of session context.

### Tech stack at a glance

| Layer | Tech |
|---|---|
| Backend | Python 3.10+, FastAPI, uvicorn, watchdog, asyncssh, sse-starlette, sqlite3 (stdlib) |
| Frontend | Node 18+, Vite 6, React 18, TypeScript, Tailwind, shadcn/ui primitives, Zustand, react-markdown + remark-gfm |
| Persistence | SQLite at `~/.claude/watcher/cache.sqlite` |
| LLM access | Subprocess CLI calls to `claude` and `codex` — no API keys, no cloud sync, no telemetry |
| Tests | pytest (backend, 92 passing + 1 skipped), vitest (frontend, 78 passing) |

---

## 2. DB Schema

DB lives at `~/.claude/watcher/cache.sqlite`. Schema version tracked via
`PRAGMA user_version`. All timestamps are integer milliseconds since
epoch (`int(time.time() * 1000)`) unless otherwise noted.

### `translations` (v1)

Per-text-hash translation cache. Keyed by `(source_hash, target_lang)`
so re-translating the same string is instant.

| Column | Type | Notes |
|---|---|---|
| `source_hash` | TEXT NOT NULL | Part of compound PK |
| `target_lang` | TEXT NOT NULL | Part of compound PK (e.g. `"ar"`) |
| `source_text` | TEXT NOT NULL | Original text |
| `translation` | TEXT NOT NULL | Translated body |
| `model` | TEXT NOT NULL | Provider model id |
| `created_at` | INTEGER NOT NULL | ms epoch |

**Index:** `idx_tr_created(created_at)`.

### `scratchpad` (v1)

Saved snippets / results from selection-toolbar actions
(translate / clarify / summarize / explain / glossary / comment).

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `action` | TEXT NOT NULL | `clarify`/`summarize`/`explain`/`glossary`/`comment`/`translate` |
| `source_text` | TEXT NULLABLE | The selected text input |
| `source_turn` | TEXT NULLABLE | Originating turn uuid (display anchor) |
| `result` | TEXT NOT NULL | The action result body |
| `model` | TEXT NULLABLE | Provider model id |
| `session_id` | TEXT NULLABLE | Claude session this came from |
| `created_at` | INTEGER NOT NULL | ms epoch |

**Indexes:** `idx_sp_created(created_at)`, `idx_sp_session(session_id)`.

### `settings` (v1)

Free-form key/value store.

| Column | Type | Notes |
|---|---|---|
| `key` | TEXT PK | |
| `value` | TEXT NOT NULL | |

Keys observed in code: `"provider"` (active translate / summary
provider — `claude` or `codex`).

### `summaries` (v1)

Cache of AI-generated session summaries.

| Column | Type | Notes |
|---|---|---|
| `content_hash` | TEXT PK | Hash of the transcript content |
| `session_id` | TEXT NOT NULL | |
| `summary` | TEXT NOT NULL | |
| `model` | TEXT NOT NULL | |
| `token_in` | INTEGER NULLABLE | **(declared but not populated by current code path; inferred unused)** |
| `token_out` | INTEGER NULLABLE | **(same — inferred unused)** |
| `created_at` | INTEGER NOT NULL | ms epoch |

**Index:** `idx_sum_session(session_id)`.

### `prompt_drafts` (v1)

Drafts the Prompt Writer has produced or refined.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `bucket` | TEXT NOT NULL | Source project bucket |
| `session_id` | TEXT NOT NULL | Source Claude session |
| `source_event_uuid` | TEXT NULLABLE | Source turn (when written from a turn) |
| `mode` | TEXT NOT NULL | One of the 8 writer modes |
| `context_mode` | TEXT NOT NULL | Context selection strategy |
| `rough_input` | TEXT NOT NULL | The user's typed seed |
| `generated_prompt` | TEXT NOT NULL | Final prompt produced by the LLM |
| `improvement_notes` | TEXT NULLABLE | Optional explanation block |
| `context_used` | TEXT NULLABLE | JSON-stringified context summary |
| `context_chars` | INTEGER NULLABLE | Char count of the context pack actually sent |
| `model` | TEXT NULLABLE | Provider model id |
| `created_at` | INTEGER NOT NULL | ms epoch |
| `updated_at` | INTEGER NOT NULL | ms epoch |

**Indexes:** `idx_pd_created(created_at)`, `idx_pd_session(session_id)`.

### `remote_hosts` (v1, extended through v3)

SSH/WSL hosts the watcher mirrors.

| Column | Type | Added in | Notes |
|---|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | v1 | |
| `name` | TEXT NOT NULL UNIQUE | v1 | Display label |
| `host` | TEXT NOT NULL | v1 | Hostname or IP |
| `port` | INTEGER NOT NULL DEFAULT 22 | v1 | |
| `username` | TEXT NOT NULL | v1 | |
| `key_path` | TEXT NULLABLE | v1 | Path to private key; null = ssh-agent + defaults |
| `projects_path` | TEXT NULLABLE | v1 | Override of `~/.claude/projects` |
| `home_dir` | TEXT NULLABLE | v1 | Discovered remote `$HOME` |
| `platform` | TEXT NULLABLE | v1 | `linux` / `darwin` / etc. |
| `enabled` | INTEGER NOT NULL DEFAULT 1 | v1 | Boolean flag |
| `last_synced_ms` | INTEGER NULLABLE | v1 | ms of the last successful sync |
| `last_error` | TEXT NULLABLE | v1 | Most recent connection error |
| `created_at` | INTEGER NOT NULL | v1 | |
| `updated_at` | INTEGER NOT NULL | v1 | |
| `kind` | TEXT NOT NULL DEFAULT `'ssh'` | v2 | Currently only `ssh` |
| `status` | TEXT NULLABLE | v3 | `live` / `connecting` / `reconnecting…` / `stopped` / `error` |
| `last_poll_ms` | INTEGER NULLABLE | v3 | ms of last completed poll |
| `last_event_ms` | INTEGER NULLABLE | v3 | ms of last observed remote change |
| `next_retry_ms` | INTEGER NULLABLE | v3 | When backoff will next retry |

**Index:** `idx_rh_enabled(enabled)`.

### `review_threads` (v4 + v5)

One row per review conversation.

| Column | Type | Added in | Notes |
|---|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | v4 | thread id |
| `name` | TEXT NOT NULL | v4 | Display label, auto-generated by panel |
| `provider` | TEXT NOT NULL | v4 | Reviewer LLM provider; `"codex"` in V1 |
| `project_bucket` | TEXT NULLABLE | v4 | Bucket the thread relates to |
| `claude_session_id` | TEXT NULLABLE | v4 | Anchor session UUID |
| `provider_session_id` | TEXT NULLABLE | v4 | Codex session UUID for resume |
| `created_at` | INTEGER NOT NULL | v4 | |
| `updated_at` | INTEGER NOT NULL | v4 | |
| `archived_at` | INTEGER NULLABLE | v4 | Soft-archive ms; NULL means active |
| `active_skill_id` | TEXT NULLABLE | **v5** | Skill last used to send |
| `provider_session_skill_id` | TEXT NULLABLE | **v5** | Skill the stored Codex session was created under |
| `provider_session_skill_version` | INTEGER NULLABLE | **v5** | `SKILL_VERSION` at session creation time (currently 1) |

**Indexes:** `idx_rt_bucket(project_bucket, updated_at DESC)`,
`idx_rt_active(archived_at, updated_at DESC)`.

#### How these fields drive Review Threads behavior

- **`provider_session_id`** is the Codex session UUID we tell Codex to
  *resume* on the next send. Replaced after every successful send with
  whatever Codex returned. NULL means "force cold start."
- **`active_skill_id`** records which skill was last sent under. Used
  for UI display and audit; the `(skill_id, version)` decision pair
  is the next two fields.
- **`provider_session_skill_id` + `provider_session_skill_version`**
  pin the stored Codex session to the *(skill, version)* it was
  created under. On every send the route compares these against the
  current request; if either differs, the stored session is
  discarded (cold start) and `skill_session_reset=true` is recorded
  on the reviewer message.
- **`archived_at`** flips `NULL` ↔ ms-epoch via the PATCH route.
  Default thread listing filters `archived_at IS NULL`.

### `review_messages` (v4)

Append-only log of every user/reviewer turn within a thread.

| Column | Type | Added in | Notes |
|---|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | v4 | |
| `thread_id` | INTEGER NOT NULL FK CASCADE | v4 | Parent thread |
| `role` | TEXT NOT NULL | v4 | `"user"` or `"reviewer"` |
| `content` | TEXT NOT NULL | v4 | The user's typed guidance, or the raw reviewer reply |
| `source_session_id` | TEXT NULLABLE | v4 | Claude session this exchange was about |
| `source_turn_uuid` | TEXT NULLABLE | v4 | Specific Claude assistant turn this exchange anchors to |
| `context_used_json` | TEXT NULLABLE | v4 | JSON: settings active for this exchange (see below) |
| `evidence_used_json` | TEXT NULLABLE | v4 | JSON: trimmed evidence audit snapshot (user rows only) |
| `provider` | TEXT NULLABLE | v4 | `"codex"` for V1 |
| `model` | TEXT NULLABLE | v4 | Codex-reported model (e.g. `gpt-5.5`); set on reviewer rows |
| `estimated_tokens` | INTEGER NULLABLE | v4 | UTF-8 → token heuristic for outbound packet (user rows) |
| `provider_tokens` | INTEGER NULLABLE | v4 | Real "tokens used" parsed from Codex stderr (reviewer rows) |
| `created_at` | INTEGER NOT NULL | v4 | |

**Index:** `idx_rm_thread(thread_id, created_at)`.

#### `context_used_json` shapes

User row:
```json
{
  "skill_id": "quick_review",
  "skill_version": 1,
  "reviewer_mode": "quick_review",
  "evidence_toggles": { "include_claude_turn": true, ... }
}
```

Reviewer row:
```json
{
  "skill_id": "quick_review",
  "skill_version": 1,
  "reviewer_mode": "quick_review",
  "resume_attempted": false,
  "resume_succeeded": false,
  "skill_session_reset": true
}
```

`reviewer_mode` is a **legacy alias** preserved for back-compat with
older callers / dashboards; `skill_id` is the canonical field.

#### `evidence_used_json` shape

Stored on user rows only. Compact audit snapshot — excerpts (not full
bodies), capped per type, plus byte/token counts and truncated flags.
Allows answering "what did the reviewer actually see?" forensically
without bloating the DB.

---

## 3. Migration History

Each migration runs once on a connection wrapped in a `BEGIN…COMMIT`
transaction. On any failure the transaction rolls back and the on-disk
DB is unchanged.

**Backup behavior:** Before any migration runs against an existing DB,
`server/db.py:_backup_db()` copies the file next to itself with the
suffix `.backup-pre-vN-<ISO timestamp>`. Skipped on a fresh install.

| Version | Description | Notes |
|---|---|---|
| 1 | Initial schema — `translations`, `scratchpad`, `settings`, `summaries`, `prompt_drafts`, `remote_hosts`. | Implemented as `CREATE TABLE IF NOT EXISTS` so it's idempotent. Safe on existing data. |
| 2 | `remote_hosts.kind`. | `ALTER TABLE` with a `_safe_alter` helper that swallows "duplicate column" errors so DBs already touched by older code with the same column don't break. |
| 3 | `remote_hosts.status / last_poll_ms / last_event_ms / next_retry_ms`. | Same idempotent ALTER pattern. |
| 4 | `review_threads` + `review_messages` tables. | Net-new tables — no impact on existing rows in any other table. |
| 5 | `review_threads.active_skill_id / provider_session_skill_id / provider_session_skill_version`. | All NULLable; existing rows get NULL and become "legacy" (the next send into them cold-starts and sets `skill_session_reset=true`). |

All migrations are *additive*. No column has ever been dropped or
renamed in-place; renames are done at the application layer (e.g. the
frontend rename of `reviewerMode` → `skillId` was a code-only change).

---

## 4. Review Threads Workflow

### 4.1 Opening Review

There are two trigger surfaces, both call the same store action.

#### From the per-turn action row

In `web/src/components/timeline/turn.tsx`, every assistant turn
renders an always-visible row with `[Review this] [Write prompt from
this] [Copy]`. Clicking `Review this`:

```ts
openReviewPanel({
  sourceTurnUuid: <turn uuid>,
  sourceTurnRole: "assistant",
  sourceTurnText: <markdown body of the turn>,
});
```

This sets `reviewPanel.{open,sourceTurnUuid,sourceTurnRole,sourceTurnText}`
in the Zustand store and the lazy `ReviewPanel` chunk mounts.

#### From the session toolbar

`SessionToolbar` has a `[Review]` button that anchors to the **most
recent assistant turn** in the loaded session (skipping
`is_command_artifact` and tool-only turns). If there's no assistant
turn at all, `sourceTurnUuid` ends up `null` and the panel opens with
no specific anchor.

#### What populates the Subject card

The Subject card reads `panel.sourceTurnText`. If empty, it shows a
neutral "No specific Claude result selected" line. Otherwise it
collapses to the first 4 lines / 280 chars with a **Show more / Show
less** toggle.

### 4.2 Thread selection

#### Auto-prepare ("find or create")

On panel-open, a `useRef`-gated effect runs once and calls
`ensureThread(sourceTurnUuid, sourceTurnText)`:

1. `GET /api/reviews/threads?project_bucket=…` — list active threads
   for the current bucket.
2. **Find** the most recent thread where `claude_session_id` matches
   the current session and `archived_at IS NULL`. If found, set
   `panel.threadId` to its id.
3. **Otherwise create** via `POST /api/reviews/threads` with a
   default name:
   - From a turn: `"Review: <first 60 chars of the Claude result>"`
   - From the toolbar: `"Review: <session display name>"`
4. On any error: render an inline retry banner; the panel does not
   crash.

**No reviewer LLM calls happen during open.** Auto-prepare is
strictly DB.

#### Why the thread list is hidden by default

The default workflow is single-focus: open Review on a Claude turn,
type guidance, send, get verdict + prompt. Thread management is
secondary. The thread list lives behind a `[History]` button in the
panel header.

#### How History works

The header `[History]` button opens a popover containing:
- The list of threads for the current bucket (most recent first,
  active marked with a check).
- A `+ New thread` button for explicitly starting a separate thread
  alongside whatever auto-prepare picked.

Clicking a thread row sets `panel.threadId` to that id; the messages
list reloads.

#### Switching threads

Replacing `panel.threadId` triggers a `GET
/api/reviews/threads/{id}/messages` and the chat refreshes. The
"current review only" filter (see below) still applies on the new
thread.

### 4.3 Sending a review

Composer → `onSend` → `POST /api/reviews/send`. Body:

```jsonc
{
  "thread_id": 2,
  "question": "<typed guidance, or DEFAULT_QUESTION when empty + auto>",
  "skill_id": "quick_review",
  "reviewer_mode": null,                  // legacy alias; route accepts either
  "project_bucket": "...",
  "project_cwd": "...",
  "claude_session_id": "...",
  "claude_turn_uuid": "...",
  "claude_turn_role": "assistant",
  "claude_turn_text": "...",
  "test_output": null,                    // user-pasted, optional
  "build_output": null,                   // user-pasted, optional
  "evidence": {
    "include_claude_turn": true,
    "include_git_status": true,
    "include_changed_files": true,
    "include_git_diff": true,
    "include_test_output": true,
    "include_build_output": true
  },
  "secret_override": false
}
```

#### What goes into the Review Packet

`server/review_packet.py:build_packet` produces a single string:

```
<skill instruction body>
\n--- EVIDENCE ---
PROJECT: ...
CLAUDE SESSION: ...

## SELECTED CLAUDE RESULT (assistant)
<trimmed turn text, cap = 12 KB>

## GIT STATUS
branch: main
ahead/behind upstream: +0/-0
dirty entries: 3

## CHANGED FILES
   .M server/review_packet.py
   ?? new.txt

## GIT DIFF
```diff
<trimmed, cap = 60 KB>
\```

## TEST OUTPUT
<trimmed, cap = 8 KB>

## BUILD OUTPUT
<trimmed, cap = 8 KB>

--- USER QUESTION ---
<trimmed, cap = 16 KB>
```

Caps live as constants in `review_packet.py` (`MAX_DIFF_BYTES = 60_000`
etc.). Audit excerpts (smaller caps: 4 KB diff, 2 KB else) are stored
in `review_messages.evidence_used_json`.

#### Default question + Auto Review

If the user's typed guidance is empty/whitespace, the frontend
substitutes a constant before sending:

> "Review this Claude Code result. Focus on correctness, risks,
> missing tests, scope creep, and the best next prompt to send to
> Claude Code."

The Send button gating:

| State | Send |
|---|---|
| Composer empty, Auto Review **off** | disabled |
| Composer empty, Auto Review **on** | enabled — sends `DEFAULT_QUESTION` |
| Composer non-empty | enabled — sends the typed text (trimmed) |

Auto Review is panel-scoped React state (not persisted across
reloads).

#### When the provider is called

Only after secret detection passes. If `packet.secret_hits` is
non-empty and `secret_override=false`, the route returns
**HTTP 409 SECRET_DETECTED** with the offending labels, and the
provider is **not** called. The panel surfaces the warning band and
the user can either edit the evidence or check `Override for this
send only`.

#### What gets stored

The route persists the **user message first** so the audit row
exists even if the provider call later fails:

```python
db.add_review_message(
    role="user",
    content=req.question,
    source_session_id=...,
    source_turn_uuid=...,
    context_used_json=json.dumps({skill_id, skill_version,
                                  reviewer_mode (alias),
                                  evidence_toggles}),
    evidence_used_json=json.dumps(packet.audit_snapshot),
    estimated_tokens=packet.estimated_tokens,
    provider="codex",
)
```

Then the provider call. On success, the reviewer message:

```python
db.add_review_message(
    role="reviewer",
    content=result.text,
    source_session_id=...,
    source_turn_uuid=...,
    context_used_json=json.dumps({skill_id, skill_version,
                                  reviewer_mode (alias),
                                  resume_attempted,
                                  resume_succeeded,
                                  skill_session_reset}),
    provider="codex",
    model=result.model,
    provider_tokens=result.tokens_used,
)
db.update_review_thread(
    provider_session_id=result.session_id_out,
    active_skill_id=skill_id,
    provider_session_skill_id=skill_id,
    provider_session_skill_version=SKILL_VERSION,
)
```

### 4.4 Skills and provider sessions

V1 ships **three skills** (`server/review_skills.py`):

| `skill_id` | Label | Purpose |
|---|---|---|
| `quick_review` *(default)* | Quick Review | Brief daily review |
| `critical_review` | Critical Review | Risks / weak assumptions / missing tests / scope creep — concise |
| `prompt_coach` | Prompt Coach | Help write the best next prompt |

Each ships a typed `instruction` body that:

- Names the reviewer's role.
- Forbids legacy headings (e.g. `What looks correct`,
  `Risks / weak assumptions`, `Missing tests`,
  `Scope creep warnings`, `KEY FINDINGS`,
  `RECOMMENDED NEXT STEP`, `NEXT PROMPT FOR CLAUDE CODE`,
  `IMPROVED PROMPT`).
- Specifies the canonical output format.

#### `SKILL_VERSION`

A single project-wide integer in `server/review_skills.py` (currently
**1**). Bumping it forces a fresh Codex session for **every** thread
on next send — useful when changing any skill's instruction body in a
way the reviewer would notice from memory.

#### Resume vs cold start (the reset rule)

Inside `POST /api/reviews/send`, after the user message has been
persisted but before the provider call:

```python
stored_skill_id      = thread.provider_session_skill_id
stored_skill_version = thread.provider_session_skill_version
skill_changed = (stored_skill_id != skill_id
                 or stored_skill_version != SKILL_VERSION)

session_id_in: str | None = thread.provider_session_id
if skill_changed and session_id_in is not None:
    log.info("…discarding stored Codex session…")
    session_id_in = None        # forces cold start

if session_id_in is not None:
    try:
        result = await provider_fn(prompt, session_id_in=session_id_in)
        resume_succeeded = True
    except CodexResumeFailed:
        ...                     # falls through to cold start
if result is None:
    result = await provider_fn(prompt, session_id_in=None)
```

#### `skill_session_reset` truth table

Recorded on each reviewer message's `context_used_json`.

| Stored `provider_session_id` | Stored skill | Request skill | `skill_session_reset` |
|---|---|---|---|
| NULL | (any) | (any) | `false` (no reset because no stored session) |
| set | matches request | matches `SKILL_VERSION` | `false` (resume attempted) |
| set | NULL (legacy) | (any) | **`true`** (mismatch on first compare) |
| set | different skill | (any) | **`true`** |
| set | matches request | different `SKILL_VERSION` | **`true`** |

#### How old pre-skill threads behave

Threads created before migration v5 have all three skill columns
NULL. The very first send into them under any skill will:

1. Compare `(NULL, NULL)` vs `(skill_id, SKILL_VERSION)` → mismatch.
2. Discard the stored Codex session.
3. Cold-start, get a fresh `provider_session_id`.
4. Persist the new session id alongside `(skill_id, SKILL_VERSION)`.
5. Record `skill_session_reset=true` on the reviewer message.

After that first send, the thread looks identical to a v5-native
thread.

### 4.5 Reviewer output

#### Canonical format (Critical / Quick)

```
VERDICT:
[ONE short sentence]

WHY:
- [short point]
- [short point]
- [short point — at most 3 bullets]

NEXT ACTION:
[ONE clear actionable instruction]

PROMPT TO SEND CLAUDE:
[clean Markdown prompt, no surrounding fences around the WHOLE prompt]

OPTIONAL NOTES:
[ONLY if needed; otherwise omit]
```

#### Canonical format (Prompt Coach)

```
CLARIFIED INTENT:
[1-2 sentences]

PROMPT TO SEND CLAUDE:
[full copy-ready prompt]

WHY THIS WORKS:
- [short reason]
- [short reason]
```

#### Prompt extraction

`web/src/lib/review-parser.ts` walks the reply line by line, applies
`normalizeHeadingLine` (a multi-pass strip of `#`, `**`, numeric
prefix, trailing colon/dash) until stable, and matches against an
allowlist of known headings. The parser recognizes both new and
legacy variants:

- `PROMPT TO SEND CLAUDE` (canonical, all skills)
- `NEXT PROMPT FOR CLAUDE CODE` (legacy critical/coach)
- Markdown wrappers: `## NEXT PROMPT FOR CLAUDE CODE`,
  `**6. NEXT PROMPT FOR CLAUDE CODE:**`,
  `### NEXT PROMPT FOR CLAUDE CODE`

Code-fence wrappers around the *entire* prompt body
(`` ```text … ``` ``) are stripped via `stripFences`. Inner code
blocks within the prompt are preserved.

#### Copy prompt

The `LatestPromptBox` finds the most recent reviewer message in
`visibleMessages` with a non-null parsed `nextPrompt` /
`improvedPrompt`. Its `Copy prompt` button writes
`prompt.trim()` to the clipboard — never the surrounding sections,
never the full review.

If no reviewer message has a parsed prompt, `NoPromptHint` renders
instead — there is no Copy button, by design (per spec: "do not
copy a bad guess").

---

## 5. Prompt Writer Workflow

The Prompt Writer is a **separate** sheet from Review Threads. Same
Codex/Claude provider abstraction, different purpose.

### Purpose

Take the user's rough idea + a chosen amount of session context and
produce a polished, copy-ready prompt for Claude Code. It does NOT
have a verdict / risk / "review" framing — it's a prompt forge.

### Writer modes

8 modes (`PromptWriterMode` in `web/src/lib/api.ts`):

| `mode` | Intent |
|---|---|
| `improve` | Polish wording, fix grammar, keep intent |
| `clarify` | Identify gaps, add bracketed questions |
| `developer_task` | Structured prompt for Claude Code/Codex |
| `bug_report` | Observed/Expected/Repro/Fix/Verify |
| `design` | Layout, flow, components, states |
| `critical_review` | Ask AI to challenge assumptions *(naming overlap with Review Skill — different feature)* |
| `short_command` | 1–3 sentences, ready to paste |
| `continue` | Treat as next message in this session |

### Context modes

Adaptive set: `auto`, `none`, `current_item`, `selected`,
`recent_small`, `focused`, `expanded`, `full_session`. Plus legacy
aliases `recent`, `selected_plus_nearby`, `summary`, `full_feature`
(still accepted by the backend; UI normalizes them on read).

### Auto Context behavior

`web/src/lib/context-pack.ts` defines:

- `decideAutoMode({hasSelection, hasSourceEvent, roughInput, writerMode})` —
  picks the smallest reasonable context based on entry point + input.
- `resolveContextMode(userPick, autoDecision)` — applies the user's
  override or returns the auto pick.
- `buildContextPack({...})` — produces the actual `ContextPackItem[]`
  that gets sent.

The default user-facing context mode is `"auto"` — the resolver
picks `selected` / `current_item` / `recent_small` etc. based on
how the panel was opened.

### Token / character control

`packCharCount(items)` and `estimateTokens(chars)` (≈ 4 chars/token)
drive a live size readout. `LARGE_CONTEXT_THRESHOLD = 8000` chars
triggers a confirmation prompt before sending `full_session` —
`modeRequiresConfirmation()` is the gate.

### Drafts

Every generation persists to `prompt_drafts`. The history popover in
the writer's header lists recent drafts (limited to 30 by default,
filtered by session_id when in a session). Loading a draft restores
its `mode`, `context_mode`, and `rough_input`.

### Refine

`POST /api/prompt-writer/refine` takes an existing draft + a
refinement instruction and produces a new draft (same `id` flow as
generate; the original draft row is updated).

### How it differs from Review Threads

| | Prompt Writer | Review Threads |
|---|---|---|
| Goal | Forge a prompt to send Claude | Discuss Claude's existing output |
| Output | A single draft prompt | A chat-style conversation |
| Anchor | Optional source turn | Strong source turn anchor |
| Provider continuity | None — each call is independent | Codex resume + skill versioning |
| Persistence | `prompt_drafts` | `review_threads` + `review_messages` |
| Default surface | "Write prompt from this" button | "Review this" button |

---

## 6. Remote Watcher Workflow

The watcher mirrors Claude session JSONLs from a remote machine
(SSH or WSL) so they appear next to local sessions in the sidebar.

### `remote_hosts` table

(See [DB Schema](#remote_hosts-v1-extended-through-v3) above.) The
status fields (`status`, `last_poll_ms`, `last_event_ms`,
`next_retry_ms`) are populated by the watcher loop and used by the
Settings UI to show a `live`/`error`/`reconnecting` badge per host.

### WSL discovery

`server/wsl.py` shells out to `wsl.exe` to enumerate distros, finds
each distro's `$HOME`, detects whether `sshd` is running and on what
port (parsed in Python from `ss -ltn` output rather than relying on
shell substitution, which `wsl.exe`'s argv layer was found to mangle
on Windows). The Settings UI calls `POST /api/remotes/discover-wsl`,
gets a list of `WslDistroInfo` items + ready-to-add `RemoteHostCreate`
suggestions, and offers a one-click `Add as remote` per distro.

### Remote watcher loop

`server/remote_watcher.py` runs one async task per **enabled** host.
Each maintains a persistent SSH/SFTP session via `asyncssh`, polls
every ~2s during activity, ramps to 10s when idle, and full-scans
every 30s. New bytes from each remote `~/.claude/projects/<bucket>/<session>.jsonl`
get append-fetched into the local mirror dir.

The local watchdog observer then emits SSE events for the mirror
files the same way it does for native local sessions, so frontend
behavior is identical regardless of source.

### Local mirror directory

`~/.claude/watcher/remotes/<remote_name>/` is the mirror root. Inside
it the bucket dirs follow the *same* naming scheme as
`~/.claude/projects/` so the parser can read them with no special
casing.

### Bucket namespace

Three flavors of bucket appear in the sidebar:

| Format | Origin |
|---|---|
| `<encoded-cwd>` | Local Claude Code session (CLI). |
| `ssh-<session_id>` | Cache bucket Claude Desktop creates per-pairing for an SSH session. |
| `remote:<host>:<bucket>` | Our own mirror namespace. **(inferred — confirm by inspecting the running sidebar; this format appears in code in `server/watcher.py` SSE events.)** |

The frontend `mergeByCwd` function in `web/src/lib/project-tree.ts`
merges sessions that share a normalized cwd so the user sees one
project node even when local + remote duplicates exist.

### Polling intervals (env-overridable)

| Env | Default | Meaning |
|---|---|---|
| `WATCHER_REMOTE_ACTIVE_POLL_S` | 2.0s | Cadence when activity is recent |
| `WATCHER_REMOTE_IDLE_POLL_MAX_S` | 10.0s | Cap when idle |
| `WATCHER_REMOTE_IDLE_RAMP_POLLS` | 10 | Consecutive no-change polls before stretching |
| `WATCHER_REMOTE_FULL_SCAN_S` | 30.0s | How often to listdir for new sessions |

Logged at startup: `remote-watcher tunables: active=2.0s idle_max=10.0s ramp_after=10 full_scan=30.0s`.

### Status fields

Surfaced in the Remotes Manager:

- `status`: `live` / `connecting` / `reconnecting (n)` / `stopped` / `error`
- `last_poll_ms`: ms of last completed poll
- `last_event_ms`: ms of last observed remote change
- `next_retry_ms`: when exponential backoff (capped at 60s) will retry

### Truncation / partial-line handling

`server/watcher.py:consume_new()` handles two edge cases:

- **Partial line.** If the last byte read isn't a newline, the offset
  is rewound so the next poll re-reads the partial line. (Tested in
  `tests/test_partial_line.py`.)
- **Truncation.** If the file size shrinks below the stored offset
  (the remote file was rewritten / truncated), the offset resets to
  0.

### Reliability test

`test_reliability.sh` is the manual end-to-end smoke for the remote
watcher path. It:

- Sandboxes everything under a `_watcher-test-<isoseconds>` bucket
  to avoid touching real session files.
- Toggles the host disable→enable to force a fresh full-scan (new
  buckets are otherwise picked up only by the 30s `FULL_SCAN`).
- Uses `MSYS_NO_PATHCONV=1` on Git Bash to keep WSL paths intact
  through Windows' path conversion.

---

## 7. Main UI Map

### Application shell (`App.tsx`)

```
┌──────────────────────────────────────────────────────────┐
│  Topbar                                                  │
├──────────┬───────────────────────────────────────────────┤
│ Sidebar  │ Timeline (or no-session start screen)         │
│          │   SessionToolbar                              │
│          │   <Turn>… <Turn>… (markdown + actions)        │
│          │   (Scratchpad pane — toggleable)              │
├──────────┴───────────────────────────────────────────────┤
│ StatusBar                                                │
└──────────────────────────────────────────────────────────┘
   Lazy overlays: SettingsSheet, PromptWriter, ReviewPanel
```

### Sidebar (project tree)

| Element | Purpose | Notes |
|---|---|---|
| Header `Projects N` | Project count | Hidden until first `listProjects()` resolves |
| `SidebarSkeleton` | Loading placeholder | Three fake project rows during initial load |
| Empty state card | First-run guidance | Shows when `projectsLoaded && projects.length === 0` |
| `ProjectTreeNode` | Recursive tree | Open state per bucket persisted in localStorage |
| `SessionRow` | Clickable session | Live-dot when modified <60s ago; entrypoint icon |

Backend interaction: `GET /api/projects` (initial + 30s polling).

### Timeline

| Element | Purpose |
|---|---|
| `NoSessionSelected` | Friendly start screen with bullets + Add-remote-host button |
| `TimelineSkeleton` | Loading state during session fetch / switch |
| `SessionToolbar` | Per-session actions (see below) |
| `Turn` cards | Header + markdown body + hover-overlay actions + bottom action row (assistant only) |
| `SelectionToolbar` | Floating toolbar on text selection — translate / scratchpad actions |
| `ScrollNavButtons` | Bottom-right scroll-to-top/bottom + new-message badge |

Backend interaction: `GET /api/sessions/{bucket}/{session_id}` plus
SSE for live updates.

### Assistant turn action row (always visible)

The new always-discoverable surface at the bottom of every assistant
message in `Turn.tsx`:

| Button | Action |
|---|---|
| `[ClipboardCheck] Review this` | Opens Review Panel anchored to this exact turn (sets `sourceTurnUuid` etc.) |
| `[Wand2] Write prompt from this` | Opens Prompt Writer with this turn as `sourceEventUuid` |
| `[Copy] Copy` | Writes the turn's markdown body to the clipboard |

The hover-overlay icon column is *also* still rendered (for muscle
memory) — the bottom row is the new always-visible spec.

### Session toolbar

In `web/src/components/timeline/session-toolbar.tsx`. Buttons present
in code:

| Button | Action |
|---|---|
| Translate-all | Per-turn translation pass; live counter |
| `Summarize` | `POST /api/summarize-session`; opens summary pane |
| `Write prompt` | Opens Prompt Writer with no source turn (session-wide) |
| `Review` | Opens Review Panel anchored to the *latest* assistant turn |
| Filter / search controls | Substring search + role filters + tool filter |
| `Export` | Markdown transcript download (full transcript or prompts-only) |

### Prompt Writer panel

Lazy sheet. Inputs: writer mode (8 options) × context mode (8+ aliased
options) × rough textarea + starter chips. Output: generated draft
with copy/refine/save/edit/delete + a history popover.

Backend: `/api/prompt-writer/{generate,refine,drafts,…}` with the
shape in `server/routes/prompt_writer.py`.

### Review Panel ("Review current Claude work")

```
┌────────────────────────────────────────────────────────────┐
│ HEADER  Review current Claude work · [History ▾] [codex]   │
├────────────────────────────────────────────────────────────┤
│ SetupBanner — "Preparing review thread…" / error           │
├────────────────────────────────────────────────────────────┤
│ SubjectCard — collapsed Claude turn preview                │
├────────────────────────────────────────────────────────────┤
│ OptionsRow — REVIEW SKILL pills + ☑ Auto review            │
├────────────────────────────────────────────────────────────┤
│ EvidencePanel — "Evidence and technical context · 6/6 on"  │
│                  (default-collapsed)                        │
├────────────────────────────────────────────────────────────┤
│ SecretWarning — when secret_hits non-empty                 │
├────────────────────────────────────────────────────────────┤
│ HistoryToggle — "N earlier messages hidden · Show history" │
├────────────────────────────────────────────────────────────┤
│ MessagesList — chat-style, filtered to >= openedAt         │
├────────────────────────────────────────────────────────────┤
│ LatestPromptBox — Markdown prompt + [Copy prompt]          │
├────────────────────────────────────────────────────────────┤
│ Composer — guidance textarea + Auto Review + [Send →]      │
└────────────────────────────────────────────────────────────┘
```

#### Review history popover

Anchored to the header `[History]` button. Shows previous threads
for the current bucket (one row per thread, active marked with a
check) plus a `+ New thread` action.

#### "Evidence and technical context" section

Default-collapsed card with a `N/6 on` summary in the header. When
expanded, shows the 6 evidence checkboxes (Claude result, git
status, changed files, git diff, test output, build output). The
test/build output **textareas** only render when their respective
toggles are on.

#### "Prompt to send Claude Code" box

`LatestPromptBox` — the hoisted, prominent prompt card. Renders the
parsed prompt with `ReactMarkdown` + `remarkGfm` so numbered lists,
bullets, and fenced code blocks come through as proper visuals;
inline `code` keeps its monospace pill. Copy button writes only the
trimmed prompt (never the surrounding sections, never the full
review).

### Scratchpad

A right-side pane togglable via Cmd-J / Ctrl-J. Lists results of
selection-toolbar actions (translate / clarify / summarize / explain /
glossary / comment) keyed by session.

Backend: `GET /api/scratchpad?session_id=…`,
`POST /api/scratchpad/run`, `DELETE /api/scratchpad/{id}`.

### Settings sheet

Lazy. Two sections:

| Section | What |
|---|---|
| Provider | Toggle between Claude (Haiku 4.5) and Codex (ChatGPT) for translate/summary/prompt-writer/review actions |
| Remote SSH hosts | `RemotesManager` — add / Discover WSL / test / sync / enable-disable / delete |

Backend: `/api/settings`, `/api/remotes/...`.

### Status bar

Bottom strip with project, branch, entrypoint icon, click-to-copy
session id, turn count, token usage, cost, model, provider, SSE-
connected indicator, remote-host badges. **(inferred from earlier
README content; details may have evolved.)**

---

## 8. Glossary

| Term | Definition |
|---|---|
| **ContextDesk** | Product name used in recent specs. Same app as claude-watcher; no rename in code yet. |
| **claude-watcher** | The git-repo / Python-package / log-channel name. Source of truth for paths and identifiers. |
| **Claude Code session** | One continuous Claude Code conversation, persisted as a single JSONL file under `~/.claude/projects/<bucket>/<session_id>.jsonl`. |
| **`session_id`** | UUID identifying a Claude Code session; also the JSONL filename without extension. |
| **Turn** | One user-or-assistant **event** in the JSONL with displayable text content. Tool-use and tool-result events are events but typically not standalone turns in the UI. |
| **`source_turn_uuid`** | The UUID of the specific assistant turn a Review or Prompt Writer action is anchored to. Stored on `review_messages` so the work being reviewed is identifiable later. |
| **`project_bucket`** | The folder name under `~/.claude/projects/` that groups one project's sessions. Three flavors exist (see [Bucket namespace](#bucket-namespace)). |
| **Local bucket** | An `<encoded-cwd>` bucket from a local Claude Code CLI session. |
| **Remote bucket** | A `remote:<host>:<bucket>` (or `ssh-<session_id>`) bucket sourced from a remote machine via SSH/SFTP mirror. |
| **Provider** | The LLM CLI used for non-Claude-Code-side actions. `claude` (default) or `codex`. Configured in `settings.provider` for translate/summary; Review Threads is `codex`-only in V1. |
| **`provider_session_id`** | The Codex session UUID stored on a `review_threads` row for resume continuity. |
| **`provider_session_skill_id`** | Which Review Skill the stored Codex session was created under. |
| **`provider_session_skill_version`** | The `SKILL_VERSION` integer at session creation time. |
| **`skill_session_reset`** | Boolean recorded on each reviewer message's `context_used_json`; `true` means the route discarded the stored Codex session because the (skill_id, version) didn't match. |
| **Review Thread** | One review conversation in the panel. Stored in `review_threads`; messages in `review_messages`. |
| **Review Message** | One row in `review_messages` — either a user guidance entry or a reviewer reply. |
| **Review Packet** | The complete prompt sent to Codex on each `/api/reviews/send`. Built by `build_packet()` from skill instruction + selected evidence + user question. |
| **Quick Review** | The default Review Skill — brief daily review. Format: VERDICT / WHY / NEXT ACTION / PROMPT TO SEND CLAUDE / OPTIONAL NOTES. |
| **Critical Review** | Skeptical-but-concise risk audit. Same output shape as Quick. |
| **Prompt Coach** | Helps draft a stronger prompt. Format: CLARIFIED INTENT / PROMPT TO SEND CLAUDE / WHY THIS WORKS. |
| **Review Skill** | A behavior preset for the reviewer LLM. Three in V1, listed above. |
| **`SKILL_VERSION`** | Project-wide integer (currently 1). Bumping it invalidates all stored Codex sessions across all threads on next send. |
| **Prompt Writer** | The separate sheet that drafts prompts to send TO Claude Code (see Section 5). Distinct from Review Threads. |
| **Context Pack** | The list of context items a Prompt Writer call sends to the LLM. Built by `buildContextPack()` per the chosen `context_mode`. |
| **Auto Context** | The `"auto"` context mode — the resolver picks the smallest reasonable context based on entry point + rough input + writer mode. |
| **Evidence** | What gets bundled into the Review Packet's `--- EVIDENCE ---` section: Claude result, git status, changed files, git diff, test/build output. User-toggled. |
| **Prompt to send Claude** | The clean, copy-ready prompt the reviewer produces inside the `PROMPT TO SEND CLAUDE` section. Hoisted into the prominent `LatestPromptBox`. |
| **Remote host** | An SSH-reachable machine the watcher mirrors session files from. Configured in `remote_hosts`. |
| **Mirror** | The local copy of a remote session JSONL under `~/.claude/watcher/remotes/<host>/<bucket>/<session>.jsonl`. |
| **SSE** | Server-Sent Events. The watcher streams live event updates to the frontend at `/sse/...` so the UI updates in real time. |
| **Scratchpad** | The right-pane list of results from selection-toolbar side-channel actions. Stored in the `scratchpad` table. |
| **Translation cache** | The `translations` table — keyed by `(source_hash, target_lang)` so re-translating the same string is free. |

---

## 9. Known Limitations

These are explicit gaps acknowledged in the code or commit history.
None of them block the current product workflow, but they shape what
to expect.

| # | Limitation | Where |
|---|---|---|
| 1 | **`tests/test_review_routes.py` auto-skips** when `httpx` isn't installed (Starlette's TestClient depends on it). PyPI was unreachable in the dev environment used for the V1 build. | `tests/test_review_routes.py:pytest.importorskip("httpx")` |
| 2 | **Codex-only reviewer in V1.** `REVIEW_PROVIDERS` is a dict (one new provider module + one map entry adds Gemini / Claude self-review later). | `server/providers/__init__.py` |
| 3 | **The reviewer-output parser is a heuristic.** Iterative strip-until-stable on heading lines + an allowlist of known labels. A pathological reviewer reply with truly novel formatting falls back to `RawReviewerView`; the panel never crashes. | `web/src/lib/review-parser.ts` |
| 4 | **Secret detection is a heuristic.** Catches AWS/GitHub/OpenAI/Slack/Google/private-key-block and generic credential assignments. Other tokens may slip through; the user-supervised override is the escape hatch. | `server/review_packet.py:SECRET_PATTERNS` |
| 5 | **Auto Review checkbox is panel-scoped React state**, not persisted across reloads. The infrastructure exists in `lib/persisted-state.ts` if it ever needs to be persisted. | `web/src/components/review-panel/review-panel.tsx` |
| 6 | **`SKILL_VERSION` is project-wide, not per-skill.** Bumping it forces a fresh session for every thread on next send, even threads using a skill whose body didn't actually change. | `server/review_skills.py:SKILL_VERSION` |
| 7 | **Pre-skill messages render as Critical Review** by default. Messages stored before the skills feature have `skill_id=null`; the renderer falls back to `critical_review`, which is the closest legacy match but may be wrong if the original send was actually under coach. | `web/src/components/review-panel/review-panel.tsx:reviewerModeFromMessage` |
| 8 | **No cross-session search.** Search is per-session (substring + role filters) only. | UI inspection |
| 9 | **No semantic search.** No embedding store; only string-match search exists. | UI inspection |
| 10 | **No packaging / distribution.** The app runs as `uvicorn server.main:app` + a built `web/dist`. No installer, tray app, or PyInstaller bundle. | Confirmed by README "Quick start" section |
| 11 | **`summaries.token_in / token_out` columns appear unused** by current code paths. **(inferred)** | `server/db.py:_MIGRATION_V1_STMTS` |
| 12 | **Hover-overlay action column on turns is redundant** with the new always-visible bottom row for assistant turns. Kept to preserve muscle memory. | `web/src/components/timeline/turn.tsx` |
| 13 | **Provider session is a single state machine** — V1 doesn't track session health beyond store/clear. Resume failures fall back to cold; that's it. | `server/routes/reviews.py:send` |
| 14 | **Logger configuration is `watcher.*` only.** Uvicorn's own loggers are not reconfigured. | `server/log_config.py` |

---

## 10. Developer Commands

### Backend

```bash
# from repo root, with .venv activated:
uvicorn server.main:app --reload --port 8765   # dev server (auto-reload)
uvicorn server.main:app --port 8765            # production-ish

# tests
.venv/Scripts/pytest                           # all backend tests
.venv/Scripts/pytest tests/test_review_skills.py   # one file
.venv/Scripts/pytest -x                        # stop on first failure

# DB schema introspection (no helper script — run inline):
.venv/Scripts/python -c "
import sqlite3, os
db = os.path.expanduser('~/.claude/watcher/cache.sqlite')
conn = sqlite3.connect(db)
print('user_version:', conn.execute('PRAGMA user_version').fetchone()[0])
"
```

### Frontend (`cd web/`)

```bash
npm run dev                # vite dev server (HMR)
npm run build              # production build → web/dist/
npm test                   # vitest, run-once
npm test -- --watch        # watch mode
npx tsc --noEmit           # type check, no emit
npx vite build             # alias of npm run build
```

### Smoke checks after a build

1. `npx vite build`
2. Restart the backend if migrations changed (`server.main` runs
   migrations on import, which uvicorn does on each fresh boot).
3. Hard-reload the browser tab (Ctrl-Shift-R / Cmd-Shift-R) — Chrome
   caches the previous chunk hashes aggressively.
4. Verify the served bundle hash matches the build:
   ```bash
   curl -s http://127.0.0.1:8765/ | grep -oE 'index-[A-Za-z0-9_-]+\.js'
   ls web/dist/assets/ | grep '^index-.*\.js'
   ```
5. Confirm the schema version with the inline command above.

### Real-Codex smoke (uses your subscription tokens)

```bash
echo "say hi in 5 words" | codex exec --skip-git-repo-check -s read-only \
  -c 'model_reasoning_effort="low"' -c 'web_search="disabled"' -
```

---

## 11. Recommended Next Steps

A short, prioritized list — not a roadmap.

1. **Use Review Threads in real work for a week.** Only fix friction
   the live workflow surfaces. Most of the recent UX changes were
   spec-driven; real usage will tell you which ones actually pay
   off.
2. **`pip install httpx`** on the dev environment and unskip
   `tests/test_review_routes.py`. The test file is ready; the skip
   is purely environmental.
3. **Loading / persisted UI state** — verify the existing
   `web/src/lib/persisted-state.ts` set still covers everything you
   want to survive a reload. Auto Review is a candidate.
4. **Cross-session search** — only when the per-session filter
   becomes the limiter. Defer until then.
5. **Auto-summary on session open** — only if you find yourself
   re-running Summarize manually a lot.
6. **Packaging / distribution** — only if you want to share the app
   beyond the dev machine. PyInstaller / Tauri / Electron-shell are
   the realistic shapes; pick when the use case is concrete.

Stop adding new Review features. The architecture (skills + skill
versioning + provider session reset + robust parser + Markdown
prompt box + per-turn anchor) is already past the point where
returns diminish.

> **Future direction for skills (not scheduled):** see
> [`docs/SKILLS_TODO.md`](./SKILLS_TODO.md) for the planned move
> from hard-coded Python skills to file-based, editable Markdown
> skills (built-in / user / project scopes, content-addressed
> `skill_hash`, hash-based session reset). No code change yet —
> the document is the brief for a future session.

---

## Document maintenance

When code changes meaningfully, update:

| If you change… | Update this section |
|---|---|
| Any DB migration | [§ 2](#2-db-schema), [§ 3](#3-migration-history) |
| Skill instruction or `SKILL_VERSION` | [§ 4.4](#44-skills-and-provider-sessions), [§ 8](#8-glossary) |
| Review route shape | [§ 4.3](#43-sending-a-review) |
| New UI surface | [§ 7](#7-main-ui-map) |
| New limitation discovered | [§ 9](#9-known-limitations) |
