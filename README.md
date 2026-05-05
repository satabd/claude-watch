# Claude Watcher

A standalone viewer for your Claude Code sessions. Watches `~/.claude/projects/`,
mirrors the active conversation, and lets you act on any text without polluting
the running session's context.

## What it does

- **Live mirror** of every Claude Code session on this machine — local CLI,
  Claude Desktop, and remote SSH sessions all show up.
- **Per-section translate toggle** — click ⇄ on any turn to flip it to Arabic.
  Translations are cached in SQLite so re-toggling is instant and free.
- **Selection popover** — highlight any text in the timeline, get a floating
  toolbar with **Translate / Clarify / Summarize / Explain code / Glossary /
  Comment** actions. Results land in the scratchpad, never in the original
  Claude session.
- **Toggleable scratchpad** (Cmd-J / Ctrl-J) — your action history with copy
  and delete buttons.

All LLM calls go through the `claude` or `codex` CLI as a subprocess, so it uses
your existing OAuth/ChatGPT subscription. No API keys needed.

## Picking a provider

Click the provider chip in the topbar (or the gear icon) to open Settings:

| Provider | Speed | Auth | Notes |
|---|---|---|---|
| **Claude (Haiku 4.5)** | ~13–17s | `claude` CLI OAuth | Default. Reliable Arabic. |
| **ChatGPT (Codex CLI)** | ~8–11s | `codex` CLI ChatGPT sub | ~35% faster. Requires codex-cli ≥ 0.128. |

If Codex rejects models, run `npm install -g @openai/codex` to upgrade.

Selection is per-machine (saved in `~/.claude/watcher/cache.sqlite`). Cached
translations are reused regardless of which provider produced them — the cache
is content-addressed by `sha256(source_text)` + target language.

## Requirements

- Python 3.10+
- Node 18+
- `claude` CLI installed and signed in (verify with `claude --version`)
- (Optional) `codex` CLI installed and signed in to ChatGPT — only needed if
  you want to use the ChatGPT provider. Install/upgrade with
  `npm install -g @openai/codex`.

## Run it

```bash
# Windows
start.bat

# macOS/Linux
./start.sh
```

First run installs the Python venv, installs Node deps, builds the web app,
and starts the server. Subsequent runs skip straight to starting.

Open <http://localhost:8765>.

## Dev mode (hot reload)

```bash
# Two windows: uvicorn --reload + vite dev server
dev.bat
```

Then open <http://localhost:5174>. The Vite dev server proxies `/api` and
`/sse` to `:8765`.

## How it works

```
~/.claude/projects/**/*.jsonl      <-- Claude Code writes here, append-only
        |
        v  watchdog (Python)
   parse_line()  (pydantic-style)
        |
        v  asyncio Queue / broadcaster
        |
        v  /sse/live  (sse-starlette)
        |
   <===== HTTP boundary =====>
        |
        v  EventSource (browser)
   zustand store
        |
        v  React + shadcn/ui timeline
```

### File layout

```
claude-watcher/
├── server/
│   ├── main.py           FastAPI entry
│   ├── watcher.py        watchdog + JSONL byte-offset tail
│   ├── parser.py         JSONL → typed events (schema variants tolerant)
│   ├── projects.py       bucket discovery, cwd decoding
│   ├── actions.py        provider-agnostic prompts (translate/clarify/...)
│   ├── providers/        per-provider subprocess runners
│   │   ├── claude_provider.py
│   │   └── codex_provider.py
│   ├── db.py             SQLite cache (translations + scratchpad + settings)
│   └── routes/           projects, stream (SSE), actions, settings
└── web/
    ├── src/
    │   ├── App.tsx
    │   ├── components/
    │   │   ├── topbar.tsx
    │   │   ├── sidebar.tsx
    │   │   ├── timeline/        Turn, ToolCard, SelectionToolbar
    │   │   ├── scratchpad.tsx
    │   │   └── ui/              shadcn primitives
    │   ├── store/app.ts         zustand
    │   └── lib/                 api, sse, utils
    └── tailwind.config.js + globals.css
```

## Data locations

- Transcripts read from: `~/.claude/projects/<bucket>/<session-id>.jsonl`
- Cache: `~/.claude/watcher/cache.sqlite`
  - `translations(source_hash, target_lang, ...)` — content-addressed cache
  - `scratchpad(action, source_text, result, session_id, ...)`

Wipe the cache with `del %USERPROFILE%\.claude\watcher\cache.sqlite` (or `rm`
on Unix).

## Notes on Claude's session storage

Bucket folder names follow two patterns:

- **Local sessions:** `D--VibeProjects-ClaudePlugins/` — the cwd with `:`,
  `/`, `\` replaced by `-`.
- **Remote SSH (Claude Desktop):** `ssh-<connection-uuid>/` — keyed by the
  SSH endpoint registration, not the remote path. The actual cwd is in every
  event line's `cwd` field.

Each event line in JSONL is one of: `user`, `assistant`, `system`,
`attachment`, `queue-operation`, `last-prompt`, `ai-title`, `pr-link`,
`file-history-snapshot` (legacy CLI). The parser keeps unknown types in
`raw` for forward compatibility.

## Two-tier model strategy (Claude provider)

- **Translate / Summarize / Glossary** → `claude-haiku-4-5` (cheap, fast)
- **Clarify / Explain code** → `claude-sonnet-4-6` (better reasoning)

For Codex, model selection is left to the CLI's default (typically the latest
model available to your ChatGPT account). Edit `server/providers/__init__.py`
to override.

## Keyboard

| key | action |
|---|---|
| `Cmd-J` / `Ctrl-J` | toggle scratchpad |
| select text | floating action toolbar |
| hover a turn | reveals translate toggle in the corner |
