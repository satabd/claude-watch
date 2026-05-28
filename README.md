# Claude Watcher

> A standalone, local viewer for your Claude Code sessions. Mirrors every active conversation in real time, lets you translate / clarify / summarize / export without polluting the running session, and bridges remote work via SSH so sessions running in WSL or on a dev box show up alongside local ones.

[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Node](https://img.shields.io/badge/node-18%2B-339933.svg?logo=node.js&logoColor=white)](https://nodejs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-61dafb.svg?logo=react&logoColor=black)](https://react.dev/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5-3178c6.svg?logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![SQLite](https://img.shields.io/badge/SQLite-cache-003b57.svg?logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Local-first](https://img.shields.io/badge/data-local%20only-7c3aed.svg)](#privacy--data-flow)
[![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux%20%7C%20WSL-blue.svg)]()

Runs entirely on your machine. No API keys, no cloud sync, no telemetry. Uses the OAuth/subscription you already have in the `claude` and/or `codex` CLIs.

---

## Table of Contents

- [Features](#features)
- [Quick start](#quick-start-5-minutes)
  - [Requirements](#requirements)
  - [Install](#install)
  - [Run](#run)
  - [Configure providers](#configure-providers-one-time)
  - [Add a WSL or remote machine](#add-a-wsl-or-remote-machine-optional)
- [Daily-use cheatsheet](#daily-use-cheatsheet)
- [Architecture](#architecture-for-developers)
  - [Pipelines](#pipelines)
  - [Layout](#layout)
  - [Database schema](#database-schema)
  - [Provider abstraction](#provider-abstraction)
  - [Configurable poll intervals](#configurable-poll-intervals)
- [Privacy / data flow](#privacy--data-flow)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgments](#acknowledgments)

---

## Features

- **Live mirror.** Every Claude Code session on this machine — local CLI, Claude Desktop, and remote SSH sessions — appears in the sidebar and updates in real time as new turns arrive.
- **Per-turn translate toggle** (Arabic by default). Click ⇄ on any turn, it flips to Arabic in place. Cached forever in SQLite by `sha256(text)`, so re-toggling is instant and free.
- **Selection popover.** Highlight any text in the timeline → floating toolbar with **Translate / Clarify / Summarize / Explain code / Glossary / Comment / Turn into prompt**. Results land in the scratchpad, not the original Claude session.
- **Context-Aware Prompt Writer.** Sheet UI with 8 writer modes (Improve / Clarify / Developer Task / Bug Report / Design / Adversarial Review / Short Command / Continue Conversation) × 8 context modes (auto / none / current item / selected / recent / focused / expanded / full session). Auto-mode picks the smallest needed context based on entry point + rough input + writer mode.
- **AI session summary.** One click → markdown summary of an entire session, cached by transcript hash.
- **Multi-host SSH.** Add a remote (or one-click "Discover WSL"); the watcher keeps a persistent SSH/SFTP connection, polls every 2s, append-fetches new bytes, and feeds them into the same SSE stream as local sessions.
- **VS Code–style status bar** with project / branch / entrypoint / session id (click-to-copy) / turn count / token usage / cost / model / provider / live indicator.
- **Inline review chat.** Per-turn Review button opens a chat thread under the assistant turn — review prompts versioned, evidence toggles, copy-prompt support. Backed by `codex resume` for streaming review sessions.
- **Next Prompt Coach.** Inline review skill that suggests what to ask Claude next based on the current turn.
- **Export** — full transcript or "prompts only" (numbered list of your inputs) as Markdown.

---

## Quick start (≈ 5 minutes)

### Requirements

| Component | Why |
|---|---|
| Python 3.10+ | Backend (FastAPI + watchdog + asyncssh) |
| Node 18+ | Frontend (Vite build) |
| `claude` CLI, signed in | Translate / clarify / etc. (default provider) |
| `codex` CLI ≥ 0.128, signed in (optional) | Faster translate via ChatGPT subscription |
| `wsl` (optional) | One-click WSL distro discovery |

The watcher does not need an internet connection except when an action delegates to `claude` / `codex` (which themselves authenticate to Anthropic / OpenAI).

### Install

```bash
git clone https://github.com/satabd/claude-watch.git claude-watcher
cd claude-watcher

# Backend
python -m venv .venv
.venv/Scripts/pip install -r server/requirements.txt   # Windows
# or .venv/bin/pip install -r server/requirements.txt  # macOS/Linux

# Frontend
cd web
npm install
npm run build
cd ..
```

### Run

Production (single process serves both API and built UI):

```bash
.venv/Scripts/python -m uvicorn server.main:app --port 8765
```

Open `http://localhost:8765`.

There are also `start.bat` (Windows) and `start.sh` (Unix-y) wrappers that do the same with sensible defaults, plus `dev.bat` that runs the backend + Vite dev server in two windows for fast iteration.

### Configure providers (one time)

1. Open Settings (gear icon).
2. Pick **Claude** (default, ~13–17 s per translation) or **ChatGPT/Codex** (~8–11 s, requires `codex` CLI ≥ 0.128 and a ChatGPT subscription).
3. Translation cache is shared across providers.

### Add a WSL or remote machine (optional)

1. Settings → Remote SSH hosts → **Discover WSL** (auto-finds your distros) *or* **Add host** for any SSH-reachable box.
2. **Test** → verifies auth + locates `~/.claude/projects/` + reports bucket count.
3. **Sync now** for the first sync; from then on the watcher tails it live with the same latency as a local session (~2 s).

For WSL specifically, `Discover WSL` reads the configured port from `sshd_config` (e.g. `Port 2222`), detects the username via `whoami`, and pre-fills the right `host=127.0.0.1` (since WSL2 forwards localhost IPv4 only — `localhost` would bind to IPv6 by default and time out).

---

## Daily-use cheatsheet

| Shortcut / button | Action |
|---|---|
| Cmd-F (Ctrl-F) | Search within session |
| Cmd-J (Ctrl-J) | Toggle scratchpad |
| Cmd-Enter (Ctrl-Enter) in Prompt Writer | Generate |
| ⇄ on a turn | Translate just that turn (cached) |
| **Translate all** | Translate every turn; **Hide all** to revert |
| Selection → 🪄 **Turn into prompt** | Open Prompt Writer with that text as `selected` |
| ⇧ on session toolbar | Per-tool/role filter chips |
| Settings → Provider chip | Switch Claude ⇄ Codex |
| **Review** on a turn | Inline review chat under that turn |

---

## Architecture (for developers)

```
~/.claude/projects/            <-- where Claude Code itself writes JSONLs
  ├── D--VibeProjects-Foo/     local sessions, encoded-cwd buckets
  └── ssh-<session_id>/        Claude Desktop's mirror of a remote session

~/.claude/watcher/
  ├── cache.sqlite             watcher's data (translations, scratchpad,
  │                            prompt drafts, summaries, remote_hosts,
  │                            settings, review threads/messages)
  └── remotes/<host_name>/     append-only mirrors of remote ~/.claude/projects/
        └── <bucket>/<session>.jsonl
```

### Pipelines

```
                   local + remote-mirror trees
                              │
                       watchdog observer    ◄───── filesystem events
                              │
                  byte-offset tail per file (in-memory)
                              │
                       parser.parse_line()
                              │
                       async broadcaster
                              │
                              ▼
                          SSE stream
                              │
                              ▼
                   browser → store.appendEvent
                              │
                              ▼
                          Timeline live-tail
```

Remote sessions reach this same pipeline via:

```
RemoteWatcherManager (one task per enabled host)
        │
        ▼
   persistent asyncssh connection
        │
        ▼  every 2s (or 10s during idle)
        ▼  every 30s for full-scan (catches new sessions)
        │
        ▼
   SFTP stat / append-fetch (seek+read)
        │
        ▼
   write to ~/.claude/watcher/remotes/<host>/<bucket>/<session>.jsonl
        │
        ▼  (watchdog observer picks up the modify event)
        ▼
   bucket name namespaced as `remote:<host>:<bucket>`
        │
        ▼
   SSE stream → browser (same as local)
```

### Layout

```
server/
├── main.py                FastAPI app + lifespan (starts watcher + manager)
├── parser.py              Tolerant JSONL → Event dataclass
├── projects.py            Bucket discovery, session_meta, mirror integration
├── watcher.py             watchdog observer + byte-offset tailer + SSE bus
├── remotes.py             SSH/SFTP connection (asyncssh) + manual sync
├── remote_watcher.py      Per-host background tailer + manager
├── wsl.py                 wsl --list discovery + sshd_config probing
├── actions.py             Translate / clarify / summarize / explain /
│                          glossary / prompt-writer / refine prompts
├── review_packet.py       Evidence packet builder for Review threads
├── review_skills.py       Versioned review skills (Next Prompt Coach, etc.)
├── git_capture.py         Capture git state for review evidence
├── providers/             { claude, codex } x { fast, smart } subprocess wrappers
├── routes/                FastAPI routers (projects, stream, actions,
│                          settings, prompt_writer, remotes, reviews)
└── db.py                  SQLite (translations, scratchpad, summaries,
                           prompt_drafts, remote_hosts, settings,
                           review threads + messages)

web/
├── index.html
├── vite.config.ts
└── src/
    ├── main.tsx
    ├── App.tsx                       3-pane layout, mounts everything
    ├── lib/
    │   ├── api.ts                    typed client for every backend endpoint
    │   ├── sse.ts                    EventSource wrapper with auto-reconnect
    │   ├── context-pack.ts           prompt-writer context builder + auto-mode
    │   ├── project-tree.ts           sidebar tree (subfolder + worktree
    │   │                             grouping, dedup by session_id)
    │   ├── session-display.ts        ai_title / first_prompt fallback
    │   └── session-stats.ts          turn / token / cost aggregation
    ├── store/app.ts                  zustand store (events, filters,
    │                                 prompt-writer, settings, summary)
    └── components/
        ├── topbar.tsx                  ai_title + cwd + provider chip + buttons
        ├── status-bar.tsx              VS Code-style 24px footer
        ├── sidebar.tsx                 ProjectTreeNode (recursive)
        ├── scratchpad.tsx
        ├── settings-sheet.tsx          + RemotesManager mount
        ├── remotes/remotes-manager.tsx Discover WSL + per-host CRUD + status
        ├── prompt-writer/
        │   └── prompt-writer.tsx       Sheet w/ context view, modes, output
        └── timeline/
            ├── timeline.tsx              auto-scroll, scroll-nav buttons
            ├── session-toolbar.tsx       Translate-all, Search, Summarize,
            │                             Filters, Stats popover, Export
            ├── selection-toolbar.tsx     hover popover on text selection
            ├── turn.tsx                  per-turn render + translate toggle
            ├── tool-card.tsx             collapsible tool_use card
            └── review/                   inline Review chat panel
```

### Database schema

```
SQLite at ~/.claude/watcher/cache.sqlite:

  translations(source_hash, target_lang, source_text, translation, model, created_at)
    PRIMARY KEY (source_hash, target_lang)

  scratchpad(id, action, source_text, source_turn, result, model, session_id, created_at)

  summaries(content_hash PK, session_id, summary, model, created_at)

  prompt_drafts(id, bucket, session_id, source_event_uuid, mode, context_mode,
                rough_input, generated_prompt, improvement_notes,
                context_used JSON, context_chars, model, created_at, updated_at)

  remote_hosts(id, name UNIQUE, host, port, username, key_path, projects_path,
               home_dir, platform, enabled, last_synced_ms, last_error, kind,
               status, last_poll_ms, last_event_ms, next_retry_ms,
               created_at, updated_at)

  review_threads(id, bucket, session_id, source_event_uuid, skill, prompt_version,
                 provider_session_id, created_at, updated_at)

  review_messages(id, thread_id, role, content, model, created_at)

  settings(key PK, value)
```

All schema changes use additive `ALTER TABLE ... ADD COLUMN` migrations on startup (idempotent — safe to run on a fresh DB or one with prior columns).

### Provider abstraction

`server/providers/__init__.py` exposes `resolve(provider, tier)` returning a function `run(prompt, *, model=None) -> (text, model_used)`. Two providers ship today:

| Provider | CLI | Auth | Notes |
|---|---|---|---|
| `claude` | `claude -p --model …` | OAuth | Default. `claude-haiku-4-5` for fast tier, `claude-sonnet-4-6` for smart. |
| `codex` | `codex exec` | ChatGPT | Adds `-c model_reasoning_effort="low" -c web_search="disabled"` for ~33% speed-up. Parses output from stderr (the codex CLI's structured response channel since v0.128). |

Adding a third provider = a new `providers/<name>_provider.py` exposing an async `run(...)` plus an entry in `PROVIDERS` and `DEFAULT_MODELS`. No other layer cares.

### Configurable poll intervals

Set in the environment before starting the server:

```bash
export WATCHER_REMOTE_ACTIVE_POLL_S=2.0      # poll cadence when active
export WATCHER_REMOTE_IDLE_POLL_MAX_S=10.0   # cap when idle
export WATCHER_REMOTE_IDLE_RAMP_POLLS=10     # consecutive no-change polls before stretching
export WATCHER_REMOTE_FULL_SCAN_S=30.0       # how often to listdir for new sessions
```

Logged at startup so you can confirm the active values:

```
INFO  remote-watcher tunables: active=2.0s idle_max=10.0s ramp_after=10 full_scan=30.0s
```

---

## Privacy / data flow

| Data | Where it goes |
|---|---|
| Session JSONLs (local) | Read from `~/.claude/projects/` only. Never modified. |
| Session JSONLs (remote) | Read via SFTP from the remote's `~/.claude/projects/`. Never modified on the remote. Mirrored to `~/.claude/watcher/remotes/<host>/`. |
| Translations / clarifications / summaries / generated prompts | Sent to the active provider's CLI as `stdin`. The provider authenticates with Anthropic / OpenAI; the watcher itself doesn't talk to either. |
| Cache | `~/.claude/watcher/cache.sqlite` — never leaves the machine. |
| Telemetry | None. There is no analytics or "phone home". |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev workflow (live-reload backend + Vite dev server), the test rule, and the architectural background — including the "How sessions actually flow across machines" investigation that motivated the multi-host SSH support and the `ssh-<session_id>` cache analysis.

Project reference docs (deep dive on internals): [docs/PROJECT_REFERENCE.md](docs/PROJECT_REFERENCE.md).

Issues and PRs welcome — especially around new providers, additional review skills, and remote-host platforms beyond WSL/Linux.

---

## License

[MIT](LICENSE) — see the LICENSE file for the full text. You are free to use, modify, and redistribute this software with attribution.

---

## Acknowledgments

Built to scratch a specific itch: "why doesn't my Claude Watcher show the same sessions as Claude Desktop's sidebar?" The investigation that produced the multi-host SSH support and the `ssh-<session_id>` cache analysis is discussed in [CONTRIBUTING.md](CONTRIBUTING.md) → "How sessions actually flow across machines".
