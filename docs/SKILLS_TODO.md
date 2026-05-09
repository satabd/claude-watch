# Skills — Future Direction

**Status:** TODO. Not scheduled. **Do not build yet.**

This document captures the architectural intent for moving review skills
from hard-coded Python into editable file-based skills. It is a planning
artifact — *no code change is required by this document landing*. The
current implementation in `server/review_skills.py` continues to be the
source of truth until a file-based loader is explicitly built.

---

## Why this is on the list

Today, every review skill (Quick Review, Critical Review, Prompt Coach,
Next Prompt Coach) is a hard-coded Python `ReviewSkill` dataclass with
its instruction body as a string literal. Adding, tuning, or
project-customizing a skill requires a code change + commit + restart.

The desired model is closer to how ChatGPT custom GPTs / Claude
Projects / OpenClaw skills work: skills are **content**, not code. A
user (or a project) can drop a Markdown file into a known directory
and the app picks it up.

When this lands, the inline Discuss surface and the side panel's skill
pills both source from the same loader; the architecture below stays
the same regardless of which surface is calling.

---

## Target shape

### Storage

Three roots, scanned in priority order:

| Scope | Location | Wins when… |
|---|---|---|
| **Project** | `<repo>/.claude-watcher/skills/*.md` | A specific project wants its own variant — overrides built-in and user. |
| **User** | `~/.claude/watcher/skills/*.md` | The user's personal customizations — overrides built-in. |
| **Built-in** | shipped with the app (likely `server/skills/*.md` packaged into the install) | Fallback if neither of the above defines an id. |

Resolution: when two scopes define the same `id`, the higher-priority
scope wins. The user sees one merged registry.

### File format

Markdown with YAML frontmatter:

```markdown
---
id: next_prompt_coach
label: Next Prompt Coach
category: review            # review | review-advanced | prompt-writer | (future)
version: 2                  # bumps when the body changes meaningfully
default_for_inline: true    # optional, only one skill per category may set this
default_for_panel: false
---

You are the NEXT PROMPT COACH for a developer using Claude Code. Your
job is to be a prompt strategy partner — NOT a code auditor.

…the rest of the instruction body, exactly as it would be sent to the
reviewer LLM today…
```

The body after the frontmatter IS the instruction. No templating in
v1 — that's a deliberate non-goal to keep the loader simple.

### Skill hash

A new field, `skill_hash`, derived from the file content (e.g. SHA-256
of the canonical bytes — frontmatter + body, or just body, decided at
implementation time). Computed once on load and attached to the
`ReviewSkill` instance.

The hash is what the **provider session reset rule** keys off going
forward — replacing the current `SKILL_VERSION` integer with a
content-addressed identifier. Editing a skill file *automatically*
invalidates stored Codex sessions on next send because the hash
changes; no human-managed version bump.

### DB additions

`review_threads` already has `provider_session_skill_id` and
`provider_session_skill_version` (INTEGER). The migration adds:

- `provider_session_skill_hash TEXT` (NULL on legacy rows)

The reset rule extends to:

```
skill_changed = (
    stored_skill_id != skill_id
    OR stored_skill_hash != skill_hash
)
```

`provider_session_skill_version` becomes redundant once `skill_hash`
is in place. Keep the column for a transition period; new writes set
it to a synthetic value (e.g. `1` for all file-based skills) and the
reset rule prefers the hash. Eventually drop it in a future migration.

### Loader behavior

On backend startup:

1. Walk built-in / user / project skill directories in that order.
2. For each `*.md` file: parse frontmatter; if `id` collides with an
   already-loaded skill, the later (higher-priority) one wins.
3. Compute `skill_hash` for each.
4. Build the runtime `SKILLS` dict the same shape as today.
5. **Fallback path:** if a skill referenced in code (`next_prompt_coach`,
   `quick_review`, etc.) is not present in any directory, fall back
   to the hard-coded definition in `server/review_skills.py`. This is
   the safety net for the transition period and for users who run the
   app with no skill files at all.

The route layer doesn't change — it still calls
`resolve_skill_id(...)` and `get_skill(...)`; only the registry's
contents are now loader-populated rather than code-defined.

### Hot reload (optional, later)

V1 of the file-based loader can be load-once-at-startup. A nice
follow-up is hot-reloading on file modification (watchdog already
runs in this app for session JSONLs, so the plumbing exists), but
**only if** the implementation cost is small. Otherwise punt to
restart-after-edit, which is fine for a developer tool.

---

## Migration plan (when this gets built)

1. **Build the loader** as additive code. Hard-coded skills stay in
   `server/review_skills.py`. The loader returns its own `SKILLS`
   dict; the merger picks file-based first, hard-coded as fallback.
   No behavior change for users with no skill files.
2. **Ship one skill** as a Markdown file (e.g. `next_prompt_coach.md`)
   alongside the existing hard-coded version. Verify the loader
   resolves to the file. Verify the hash-based reset rule fires
   correctly when the file is edited.
3. **Move all four skills** to Markdown. Hard-coded definitions stay
   for one release as the documented fallback.
4. **Add `provider_session_skill_hash`** column (migration v6) and
   update the reset rule to include the hash.
5. **Remove the hard-coded skill bodies** in a later release once
   confidence is high. Keep the shape definitions
   (`ReviewSkill`, `SKILLS_FALLBACK`, etc.) for the loader's "no
   skills found anywhere" emergency path.

Each step is shippable on its own; nothing requires a flag day.

---

## Explicit non-goals (for this iteration)

- **No skill editor in the UI.** Editing is via a text editor on disk.
- **No remote skill repository.** Local files only.
- **No templating language.** Skill body is the literal instruction.
- **No skill-level provider override.** All skills run against the
  configured review provider (Codex in V1; Gemini etc. later).
- **No DB migration of existing review_messages rows.** Old messages
  carry `skill_id` strings that may not match a current file; the
  parser already handles unknown skill ids by falling back to the
  Critical render mode.
- **No "categories" beyond the YAML frontmatter field.** The category
  is a hint for grouping in the panel UI; the loader doesn't enforce
  rules across categories.

---

## Open questions to revisit at implementation time

- **What hash function?** SHA-256 of UTF-8 bytes is the obvious
  default. Truncate to 16 hex chars for storage / log readability?
  Decide when building.
- **What happens to a thread whose stored `skill_hash` no longer
  exists in any file?** Treat as legacy; cold-start. Same code path
  as a `NULL` stored hash today.
- **Project skills directory placement.** `.claude-watcher/skills/` is
  one option; `.review-skills/` or under the existing `.claude/`
  conventions are also viable. Pick at implementation time, not now.
- **Frontmatter parsing dependency.** Add `pyyaml`? Or hand-roll a
  tiny parser for the small frontmatter shape? Hand-rolling avoids a
  new dependency for ~10 lines of code; PyYAML is more robust if the
  format ever grows.
- **Will the side panel's skill pills auto-update when a file is
  added?** Probably yes — the registry is fetched via
  `GET /api/reviews/skills` on panel open, and that already sees
  whatever the runtime registry contains. No special UI work needed.

---

## Until then

The current implementation in `server/review_skills.py` is the
source of truth. The four hard-coded skills (`quick_review`,
`critical_review`, `prompt_coach`, `next_prompt_coach`) work and
ship. Bumping `SKILL_VERSION` continues to be the manual lever for
forcing a session reset across the project when the wording of any
skill changes. That stays in place until the hash-based reset
replaces it.

When a future session decides to start this work, this document is
the brief.
