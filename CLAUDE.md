# claude-watch — how it is built and why

Read this before changing anything in `server/runtime/`.

This file is the architecture. It wins over habit, over "it worked before",
and over anything clever you think of while debugging. If a change does not
fit this file, change this file first, in a separate commit.

---

## What claude-watch is

Two programs in one codebase. Keep them apart.

```
                     claude-watch
                          |
              +-----------+-----------+
              |                       |
        SESSION VIEWER          SESSION MANAGER
              |                       |
            JSONL                   Zellij
              |                       |
         all sessions             Claude TUI
                                      |
                                      v
                                    JSONL
```

**The viewer** reads the JSONL transcript files Claude Code writes to
`~/.claude/projects/`. It shows every session on the machine, no matter who
started it. It never writes. It cannot break a conversation. This half is
deterministic and it is the part users rely on.

**The manager** starts and drives Claude sessions inside Zellij panes. It only
ever touches sessions it owns.

Do not blur these. The viewer is universal. The manager is narrow.

---

## The one rule

> Only one Claude process may write to a transcript at a time.

Two processes appending to one JSONL interleave their writes and corrupt it.
The damage is not recoverable and it is not obvious — the pane looks like it
is ignoring you, because it is replaying a broken file.

Everything below exists to serve this rule. So:

> Never start or resume a Claude process for a session while another Claude
> process for that session may still be alive.

When you cannot prove the other process is gone, **refuse**. Refusing costs the
user a click. Guessing wrong costs them a conversation.

---

## Ownership

A session is in exactly one of two states.

### Managed

claude-watch started it, or resumed it after the previous Claude process had
exited. It runs in a Zellij pane that claude-watch created.

claude-watch may: send prompts, change permission mode, answer dialogs,
restart the process, close and recreate the pane.

### External

Someone else started it — another terminal, another tool.

While its Claude process is alive it is **view-only**. No prompts. No kill. No
takeover. No exceptions.

Once its Claude process has exited, claude-watch may resume it into a pane and
it becomes managed.

```
External session
      |
      +-- Claude process alive --> VIEW ONLY
      |
      +-- Claude process exited
                |
                v
       Resume from claude-watch
                |
                v
        Zellij + Claude --resume
                |
                v
             MANAGED
```

External sessions are not second-class forever. They are second-class only
while something else is writing to them.

---

## Identity

```
session_id      durable identity of the conversation
project         the workspace it belongs to
process_id      a temporary attachment
zellij_session  a temporary attachment
zellij_pane     a disposable attachment
ownership       managed | external
```

**`session_id` is the identity. Everything else is an attachment to it.**

A pane can vanish and be rebuilt and the session is unchanged. A process can
exit and restart and the session is unchanged.

Never treat a pane name, a pane id, a PID, or anything on screen as identity.
Store attachments, re-verify them on every use, and throw them away the moment
they fail to verify.

---

## Which signals to trust

Use them in this order. Only drop to the next one when the one above cannot
answer.

1. **Transcript and session metadata** — files on disk. Deterministic.
2. **Process inspection** — Claude Code's own session registry
   (`~/.claude/sessions/<pid>.json`), `ps`, `lsof`. Facts about real processes.
3. **Zellij runtime state** — does the session exist, does the pane exist.
4. **Screen scraping** — last resort.

Screen scraping is **advisory**. It is fine for making the UI nicer: showing a
spinner, showing the permission mode, offering buttons for a question that is
on screen.

It must never be the sole reason for anything dangerous or irreversible — never
for killing a process, never for deciding who owns a session.

Rendered text changes when Claude Code's UI changes, and it breaks silently.
Never build an invariant on it.

---

## Zellij layout

One Zellij session per project. One tab and pane per Claude session.

```
zellij attach rumailahub                 <- the project
├── tab/pane  rumailahub-a1b2c3d4        <- a session (short id)
└── tab/pane  rumailahub-fix-the-login   <- or its AI title
```

A tab, not a split: a second pane in the same tab halves the width, and a
narrow Claude TUI wraps its dialogs into something unparseable.

The pane is disposable. If it disappears, rebuild it from the `session_id`.

---

## Repo layout

```
server/
  projects.py            find sessions and projects on disk
  parser.py  watcher.py  read transcripts, stream over SSE
  runtime/
    registry.py          who owns a session (process inspection)
    controller.py        the ownership state machine
    zellij.py            the Zellij CLI wrapper
  routes/                HTTP surface
web/src/                 React app
tests/                   pytest
```

`registry.py` answers "is anyone driving this session right now".
`controller.py` decides what claude-watch is allowed to do about it.
`zellij.py` knows nothing about sessions — it only drives Zellij.

---

## Running it

```bash
# backend, dev
.venv/bin/python -m uvicorn server.main:app --reload --port 8765

# tests
.venv/bin/python -m pytest tests/ -q
cd web && npx vitest run && npx tsc --noEmit

# frontend build (the server serves web/dist in production)
cd web && npm run build
```

It normally runs as a LaunchAgent bound to Wi-Fi only:

```bash
launchctl kickstart -k gui/$(id -u)/com.claudewatcher.server
tail -f ~/Library/Logs/claude-watcher.err.log
```

**After changing backend code, restart the service. After changing frontend
code, run `npm run build` too.** Otherwise you are looking at an old build and
wondering why nothing changed.

---

## Traps we have already fallen into

**Nested Claude breaks two things at once.** A `claude` started from a shell
that is already inside a Claude Code session inherits `CLAUDE_*` env. That
child writes no entry in the session registry *and* silently disables its own
transcript saving. It is invisible to us and to itself. `zellij._env_for()`
strips `CLAUDE_*` and `CLAUDECODE` for this reason — do not remove it.

**Zellij targets the wrong session when you are careless.** If
`ZELLIJ_SESSION_NAME` names a session that does not exist, Zellij silently
falls back to another one, or blocks forever. Every targeted call checks the
session is alive first and runs under a timeout. Keep it that way — the failure
mode is typing into someone else's terminal.

**Background Zellij sessions are 48 columns.** Whatever the creating process's
terminal was. Claude's TUI wraps unusably at that width. We force a real size
by briefly attaching a client on a sized pty.

**PIDs get recycled.** A registry record outlives a crash, and the OS hands that
number to something else later. A record is only believed while its PID is
still a Claude process.

**A screen can lie about being busy.** A pane blocked on a permission prompt
often still shows its spinner. A detected question always wins over "working".

**A managed pane does not inherit your terminal's file permissions.** It is
started by the LaunchAgent, so on macOS it gets that process's access rights.
An external volume your terminal can read may be invisible to the pane.
