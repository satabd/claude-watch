"""Action endpoints: translate, clarify, summarize, explain, glossary, comment."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import actions, db, providers
from .settings import get_provider

router = APIRouter()


def _resolve_provider(requested: str | None) -> str:
    p = requested or get_provider()
    if p not in providers.PROVIDERS:
        raise HTTPException(400, f"unknown provider: {p}")
    return p


class TranslateRequest(BaseModel):
    text: str = Field(..., min_length=1)
    target_lang: str = "ar"
    force: bool = False  # bypass cache
    provider: str | None = None


class TranslateResponse(BaseModel):
    translation: str
    cached: bool
    model: str
    target_lang: str
    source_hash: str


@router.post("/api/translate", response_model=TranslateResponse)
async def translate(req: TranslateRequest) -> TranslateResponse:
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "empty text")
    if not req.force:
        hit = db.get_translation(text, req.target_lang)
        if hit:
            return TranslateResponse(
                translation=hit["translation"],
                cached=True,
                model=hit["model"],
                target_lang=req.target_lang,
                source_hash=hit["source_hash"],
            )
    provider = _resolve_provider(req.provider)
    try:
        out, model = await actions.translate(text, req.target_lang, provider=provider)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    saved = db.save_translation(text, req.target_lang, out, model)
    return TranslateResponse(
        translation=out,
        cached=False,
        model=model,
        target_lang=req.target_lang,
        source_hash=saved["source_hash"],
    )


class TranslateLookup(BaseModel):
    text: str
    target_lang: str = "ar"


@router.post("/api/translate/lookup")
def translate_lookup(req: TranslateLookup) -> dict:
    """Cache-only lookup. Returns translation if cached, else null."""
    hit = db.get_translation(req.text.strip(), req.target_lang)
    if not hit:
        return {"hit": False}
    return {
        "hit": True,
        "translation": hit["translation"],
        "model": hit["model"],
        "target_lang": req.target_lang,
        "source_hash": hit["source_hash"],
    }


class BatchLookupItem(BaseModel):
    key: str  # arbitrary client key (e.g. event uuid)
    text: str


class BatchLookupRequest(BaseModel):
    items: list[BatchLookupItem]
    target_lang: str = "ar"


@router.post("/api/translate/lookup-batch")
def translate_lookup_batch(req: BatchLookupRequest) -> dict:
    """Bulk cache lookup. Returns {key -> {translation, model}} for hits only."""
    if not req.items:
        return {"hits": {}}
    # Build hash → key map (so we can return results keyed by client key)
    hash_to_keys: dict[str, list[str]] = {}
    text_by_hash: dict[str, str] = {}
    for it in req.items:
        text = it.text.strip()
        if not text:
            continue
        h = db.hash_text(text)
        text_by_hash[h] = text
        hash_to_keys.setdefault(h, []).append(it.key)

    rows = db.get_translations_for_hashes(list(text_by_hash.keys()), req.target_lang)
    hits: dict[str, dict] = {}
    for h, row in rows.items():
        for k in hash_to_keys.get(h, []):
            hits[k] = {"translation": row["translation"], "model": row["model"]}
    return {"hits": hits}


ActionName = Literal["clarify", "summarize", "explain", "glossary", "comment"]


class ScratchpadRequest(BaseModel):
    action: ActionName
    text: str = Field(..., min_length=1)
    context_text: str | None = None
    note: str | None = None  # for comment action
    source_turn: str | None = None
    session_id: str | None = None
    provider: str | None = None


@router.post("/api/scratchpad/run")
async def scratchpad_run(req: ScratchpadRequest) -> dict:
    text = req.text.strip()
    provider = _resolve_provider(req.provider)
    try:
        if req.action == "clarify":
            result, model = await actions.clarify(text, req.context_text, provider=provider)
        elif req.action == "summarize":
            result, model = await actions.summarize(text, provider=provider)
        elif req.action == "explain":
            result, model = await actions.explain_code(text, provider=provider)
        elif req.action == "glossary":
            result, model = await actions.glossary(text, provider=provider)
        elif req.action == "comment":
            # Comment: just store the note attached to the source text. No LLM call.
            result, model = (req.note or "").strip(), "user"
            if not result:
                raise HTTPException(400, "comment note is empty")
        else:
            raise HTTPException(400, "unknown action")
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    item = db.add_scratchpad(
        action=req.action,
        result=result,
        source_text=text,
        source_turn=req.source_turn,
        model=model,
        session_id=req.session_id,
    )
    return item


@router.get("/api/scratchpad")
def scratchpad_list(session_id: str | None = None, limit: int = 200) -> dict:
    return {"items": db.list_scratchpad(session_id=session_id, limit=limit)}


@router.delete("/api/scratchpad/{item_id}")
def scratchpad_delete(item_id: int) -> dict:
    ok = db.delete_scratchpad(item_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class SummarizeRequest(BaseModel):
    session_id: str
    transcript: str = Field(..., min_length=1)
    force: bool = False
    provider: str | None = None


@router.post("/api/summarize-session")
async def summarize_session(req: SummarizeRequest) -> dict:
    text = req.transcript.strip()
    if not text:
        raise HTTPException(400, "empty transcript")
    h = db.hash_text(text)
    if not req.force:
        hit = db.get_summary(h)
        if hit:
            return {
                "summary": hit["summary"],
                "cached": True,
                "model": hit["model"],
                "content_hash": h,
            }
    provider = _resolve_provider(req.provider)
    try:
        out, model = await actions.summarize_session(text, provider=provider)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    db.save_summary(h, req.session_id, out, model)
    return {"summary": out, "cached": False, "model": model, "content_hash": h}
