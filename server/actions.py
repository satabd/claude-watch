"""Action runners — translate, clarify, summarize, explain, glossary.

Provider-agnostic. The caller picks `provider` ("claude" or "codex").
"""
from __future__ import annotations

from . import db, providers

# Hard cap on input size sent to any provider for any single action
MAX_INPUT_CHARS = 60_000


def _trim(text: str) -> str:
    if len(text) > MAX_INPUT_CHARS:
        return text[:MAX_INPUT_CHARS] + "\n\n[... truncated ...]"
    return text


async def _run(provider: str, tier: str, prompt: str) -> tuple[str, str]:
    # The user's saved per-tier model choice (Settings) overrides the default.
    # Read at call time, not import time, so changing it takes effect without
    # a restart.
    override = db.get_setting(f"model_{provider}_{tier}", None)
    fn, model = providers.resolve(provider, tier, override=override)
    return await fn(_trim(prompt), model=model)


# ---- Action prompts ----

TRANSLATE_TEMPLATE = """You are a precise translator. Translate the following text to {lang_name} ({lang_code}).

Rules:
- Output ONLY the translation. No preamble, no explanation, no quotes around the output.
- Preserve formatting: keep markdown, code blocks, line breaks, lists, links exactly as in the source.
- Inside code blocks (``` fenced or `inline`), keep code as-is — do not translate identifiers, keywords, or strings.
- Preserve URLs, file paths, and proper nouns.
- Translate prose text naturally and idiomatically.

Source text:
---
{text}
---"""

LANG_NAMES = {
    "ar": "Arabic",
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "ja": "Japanese",
    "zh": "Chinese (Simplified)",
}


async def translate(text: str, target_lang: str, *, provider: str = "claude") -> tuple[str, str]:
    name = LANG_NAMES.get(target_lang, target_lang)
    prompt = TRANSLATE_TEMPLATE.format(lang_code=target_lang, lang_name=name, text=text)
    return await _run(provider, "fast", prompt)


CLARIFY_TEMPLATE = """The user is reading a transcript and wants a clarification.

They highlighted this passage:
---
{text}
---

{context_block}

Explain it clearly and concisely:
- Define any technical terms in plain language.
- Spell out anything that's implied or assumed.
- If the passage references code, files, or commands, briefly explain what they do.
- Aim for 3-6 sentences. Don't pad. Don't restate the passage.
"""


async def clarify(text: str, context_text: str | None = None, *, provider: str = "claude") -> tuple[str, str]:
    ctx = ""
    if context_text:
        ctx = f"Surrounding context (for reference, not to explain):\n---\n{context_text[:4000]}\n---\n"
    prompt = CLARIFY_TEMPLATE.format(text=text, context_block=ctx)
    return await _run(provider, "smart", prompt)


SUMMARIZE_TEMPLATE = """Summarize the following passage in 2-4 sentences. Capture the key point and any concrete decisions or actions. Skip preamble.

---
{text}
---"""


async def summarize(text: str, *, provider: str = "claude") -> tuple[str, str]:
    return await _run(provider, "fast", SUMMARIZE_TEMPLATE.format(text=text))


EXPLAIN_CODE_TEMPLATE = """Explain what this code does, step by step. Be precise and technical, but readable. If the code is in an unusual language, name it.

Code:
---
{text}
---

Provide:
1. One-sentence summary of purpose.
2. Key steps or branches.
3. Any non-obvious behavior, edge cases, or pitfalls.
"""


async def explain_code(text: str, *, provider: str = "claude") -> tuple[str, str]:
    return await _run(provider, "smart", EXPLAIN_CODE_TEMPLATE.format(text=text))


GLOSSARY_TEMPLATE = """Extract all technical terms, acronyms, jargon, library names, command names, and proper nouns from the passage below. For each, give a one-line definition or expansion.

Output as a markdown list: `- **TERM** — definition`. No preamble, no closing summary.

Passage:
---
{text}
---"""


async def glossary(text: str, *, provider: str = "claude") -> tuple[str, str]:
    return await _run(provider, "fast", GLOSSARY_TEMPLATE.format(text=text))


SESSION_SUMMARY_TEMPLATE = """You are summarizing a Claude Code coding session for a developer
who wants to remember what happened without re-reading hundreds of turns.

Below is a condensed transcript: each block is one turn (user prompt or model response),
truncated, with tool calls noted.

Produce a concise markdown summary covering:
- **Goal** (1 sentence): what the user was trying to accomplish.
- **What was built / changed** (bullets): concrete artifacts, files modified, decisions.
- **Open threads** (bullets, optional): unfinished work, follow-ups, or known issues.

Keep total length under 300 words. No preamble, no closing remarks. Do not invent details.

Transcript:
---
{text}
---"""


async def summarize_session(text: str, *, provider: str = "claude") -> tuple[str, str]:
    return await _run(provider, "smart", SESSION_SUMMARY_TEMPLATE.format(text=text))


# ---- Prompt Writer ----

PROMPT_WRITER_BASE = """You are a Context-Aware Prompt Writer. The user is mid-session
with another AI agent (Claude Code, Codex, ChatGPT, etc.) and needs help turning a
rough idea into a polished prompt that gets a great response.

Your job:
1. Preserve the user's original intention exactly. Never invent new goals.
2. Improve clarity, structure, and English. Fix awkward phrasing naturally.
3. Use the provided context to disambiguate references like "it", "this", "that".
4. Add missing technical details ONLY when strongly implied by context. Mark assumptions.
5. Avoid over-engineering. Match the prompt's length to the request's actual scope.
6. If the user is continuing a conversation, write the prompt as the next turn.
7. The original session text is read-only — never propose to modify it.

Output format (strict, no preamble, no closing remarks):

```prompt
<the polished prompt, ready to copy>
```

What I improved:
- <bullet 1>
- <bullet 2>
- <bullet 3>

Assumptions (omit this section if none):
- <assumption 1>

==== USER'S ROUGH INPUT ====
{rough_input}

==== CONTEXT PACK ({context_mode}, {context_chars} chars) ====
{context}
"""

# Per-mode addenda. Appended after the base instructions, before context.
PROMPT_WRITER_MODES: dict[str, str] = {
    "improve": (
        "MODE: Improve Prompt — Rewrite the user's rough prompt in clearer English. "
        "Keep their intention exactly; just polish wording, fix grammar, make it specific "
        "and actionable. Length should be similar to the input."
    ),
    "clarify": (
        "MODE: Clarify My Idea — Identify unclear or ambiguous parts of the rough input. "
        "Ask for missing details inline using bracketed clarifying questions like "
        "`[which file?]`. Convert into a more complete requirement that the user can "
        "fill in or edit. Output the questions inline within the prompt body."
    ),
    "developer_task": (
        "MODE: Developer Task Prompt — Convert the idea into a precise prompt for "
        "Claude Code or Codex. Use this structure:\n"
        "- Goal: 1 sentence.\n"
        "- Context: relevant files, current state, constraints.\n"
        "- Requirements: numbered.\n"
        "- Out of scope: what NOT to do.\n"
        "- Expected output: the agent should explain → implement → verify → summarize.\n"
        "- Verification: how to confirm it works."
    ),
    "bug_report": (
        "MODE: Bug Report Prompt — Convert the user's complaint into a structured bug "
        "report prompt. Sections:\n"
        "- Observed behavior\n"
        "- Expected behavior\n"
        "- Steps to reproduce (numbered)\n"
        "- Likely cause (if inferable from context)\n"
        "- Required fix\n"
        "- Verification steps"
    ),
    "design": (
        "MODE: Design Prompt — Convert the idea into a UI/UX design prompt. Cover:\n"
        "- Layout and visual hierarchy\n"
        "- User flow and interaction states\n"
        "- Components needed\n"
        "- Visual style cues from the existing codebase\n"
        "- Edge cases (empty/loading/error)\n"
        "- Accessibility considerations"
    ),
    "critical_review": (
        "MODE: Adversarial Review Prompt — Produce a prompt that asks an AI agent to "
        "challenge the user's idea. The output prompt should ask the agent to: "
        "find weaknesses, identify risks, surface assumptions, suggest alternatives, "
        "and flag what could go wrong. Frame as 'review my plan and push back'."
    ),
    "short_command": (
        "MODE: Short Command — Output a single concise prompt (1–3 sentences). "
        "No headers, no bullets. Just the polished instruction. Suitable for quick paste."
    ),
    "continue": (
        "MODE: Continue Conversation — Treat the rough input as a follow-up to the most "
        "recent assistant turn in the context. Resolve all pronouns ('it', 'this', 'that') "
        "using the conversation. Output the prompt as if it's the user's next message in "
        "the same session — preserve their conversational tone, no formal sections needed."
    ),
}


async def prompt_writer(
    *,
    mode: str,
    context_mode: str,
    rough_input: str,
    context: str,
    provider: str = "claude",
) -> tuple[str, str]:
    mode_addendum = PROMPT_WRITER_MODES.get(mode)
    if not mode_addendum:
        raise ValueError(f"unknown mode: {mode}")
    full_prompt = (
        mode_addendum
        + "\n\n"
        + PROMPT_WRITER_BASE.format(
            rough_input=rough_input.strip() or "(empty — infer from context)",
            context_mode=context_mode,
            context_chars=len(context),
            context=context.strip() or "(no context provided)",
        )
    )
    return await _run(provider, "smart", full_prompt)


PROMPT_REFINE_TEMPLATE = """You are refining a previously-generated prompt based on the user's feedback.

ORIGINAL POLISHED PROMPT:
```
{previous}
```

USER'S REFINEMENT REQUEST:
{refinement}

Output the revised prompt in the same format as before:

```prompt
<the revised polished prompt>
```

What changed:
- <bullet 1>
- <bullet 2>
"""


async def refine_prompt(
    *,
    previous: str,
    refinement: str,
    provider: str = "claude",
) -> tuple[str, str]:
    return await _run(
        provider,
        "smart",
        PROMPT_REFINE_TEMPLATE.format(previous=previous, refinement=refinement.strip()),
    )
