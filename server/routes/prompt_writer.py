"""Prompt Writer endpoints — generate, refine, list, get, delete drafts."""
from __future__ import annotations

import json
import re
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import actions, db
from .settings import get_provider

router = APIRouter()


def _resolve_provider(requested: str | None) -> str:
    from .. import providers as _p

    p = requested or get_provider()
    if p not in _p.PROVIDERS:
        raise HTTPException(400, f"unknown provider: {p}")
    return p


# ---- Request models ----

ModeName = Literal[
    "improve",
    "clarify",
    "developer_task",
    "bug_report",
    "design",
    "critical_review",
    "short_command",
    "continue",
]

ContextModeName = Literal[
    # New adaptive set
    "auto",
    "none",
    "current_item",
    "selected",
    "recent_small",
    "focused",
    "expanded",
    "full_session",
    # Legacy aliases (kept for old drafts and old API callers)
    "recent",
    "selected_plus_nearby",
    "summary",
    "full_feature",
]


class ContextItem(BaseModel):
    id: str
    kind: str  # "project" | "session" | "selected" | "message" | "summary" | "note"
    label: str
    text: str


class GenerateRequest(BaseModel):
    bucket: str
    session_id: str
    source_event_uuid: str | None = None
    mode: ModeName = "improve"
    context_mode: ContextModeName = "recent"
    rough_input: str = Field(default="", max_length=20_000)
    context_pack: list[ContextItem] = Field(default_factory=list)
    provider: str | None = None


class RefineRequest(BaseModel):
    draft_id: int
    refinement: str = Field(..., min_length=1, max_length=2000)
    provider: str | None = None


# ---- Helpers ----

CONTEXT_HARD_CAP = 32_000  # chars sent to the LLM


def _serialize_context(items: list[ContextItem]) -> str:
    """Render the structured Context Pack into a flat string for the LLM."""
    if not items:
        return ""
    parts: list[str] = []
    for it in items:
        text = (it.text or "").strip()
        if not text:
            continue
        parts.append(f"--- [{it.kind}] {it.label} ---\n{text}")
    return "\n\n".join(parts)[:CONTEXT_HARD_CAP]


_PROMPT_BLOCK_RE = re.compile(r"```(?:prompt)?\s*\n(.*?)\n```", re.DOTALL)
_NOTES_RE = re.compile(
    r"^(?:What I improved|What changed|Assumptions)[:\s]*\n(.+)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)


def _split_output(raw: str) -> tuple[str, str]:
    """Pull the polished prompt out of a fenced ```prompt block, plus everything else."""
    raw = raw.strip()
    m = _PROMPT_BLOCK_RE.search(raw)
    if m:
        prompt = m.group(1).strip()
        notes = (raw[: m.start()] + raw[m.end():]).strip()
        return prompt, notes
    # Fallback: entire output is the prompt
    return raw, ""


# ---- Endpoints ----


@router.post("/api/prompt-writer/generate")
async def generate(req: GenerateRequest) -> dict:
    if not req.rough_input.strip() and not req.context_pack:
        raise HTTPException(
            400, "rough_input or context_pack required (need something to write about)"
        )
    provider = _resolve_provider(req.provider)
    context_str = _serialize_context(req.context_pack)
    try:
        raw, model = await actions.prompt_writer(
            mode=req.mode,
            context_mode=req.context_mode,
            rough_input=req.rough_input,
            context=context_str,
            provider=provider,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    prompt, notes = _split_output(raw)
    draft = db.add_prompt_draft(
        bucket=req.bucket,
        session_id=req.session_id,
        source_event_uuid=req.source_event_uuid,
        mode=req.mode,
        context_mode=req.context_mode,
        rough_input=req.rough_input,
        generated_prompt=prompt,
        improvement_notes=notes or None,
        context_used=json.dumps([i.model_dump() for i in req.context_pack]),
        context_chars=len(context_str),
        model=model,
    )
    return draft


@router.post("/api/prompt-writer/refine")
async def refine(req: RefineRequest) -> dict:
    draft = db.get_prompt_draft(req.draft_id)
    if not draft:
        raise HTTPException(404, "draft not found")
    provider = _resolve_provider(req.provider)
    try:
        raw, model = await actions.refine_prompt(
            previous=draft["generated_prompt"],
            refinement=req.refinement,
            provider=provider,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    prompt, notes = _split_output(raw)
    updated = db.update_prompt_draft(
        req.draft_id,
        generated_prompt=prompt,
        improvement_notes=notes or draft.get("improvement_notes"),
    )
    return updated or draft


class UpdateDraftBody(BaseModel):
    generated_prompt: str | None = None
    improvement_notes: str | None = None


@router.patch("/api/prompt-writer/drafts/{draft_id}")
def patch_draft(draft_id: int, req: UpdateDraftBody) -> dict:
    updated = db.update_prompt_draft(
        draft_id,
        generated_prompt=req.generated_prompt,
        improvement_notes=req.improvement_notes,
    )
    if not updated:
        raise HTTPException(404, "draft not found")
    return updated


@router.get("/api/prompt-writer/drafts")
def list_drafts(session_id: str | None = None, limit: int = 30) -> dict:
    return {"items": db.list_prompt_drafts(session_id=session_id, limit=limit)}


@router.get("/api/prompt-writer/drafts/{draft_id}")
def get_draft(draft_id: int) -> dict:
    d = db.get_prompt_draft(draft_id)
    if not d:
        raise HTTPException(404, "draft not found")
    return d


@router.delete("/api/prompt-writer/drafts/{draft_id}")
def delete_draft(draft_id: int) -> dict:
    if not db.delete_prompt_draft(draft_id):
        raise HTTPException(404, "draft not found")
    return {"ok": True}
