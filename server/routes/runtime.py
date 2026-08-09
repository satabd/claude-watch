"""REST surface for Zellij runtime control and pending prompts.

The pending-prompt lifecycle is deliberately explicit — compose, review,
then send — and the send transition is guarded server-side:

    POST   /api/runtime/{bucket}/{sid}/pending          create draft
    PATCH  /api/runtime/pending/{id}                    edit draft
    DELETE /api/runtime/pending/{id}                    discard draft
    POST   /api/runtime/{bucket}/{sid}/pending/{id}/send   claim + inject

``send`` claims the row (pending -> sending) atomically before touching
Zellij, so UI retries and double-clicks cannot deliver a prompt twice.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db, projects
from ..runtime import zellij
from ..runtime.controller import (
    ControlRefused,
    TakeoverConfirmationRequired,
    controller,
)

_log = logging.getLogger("watcher.routes.runtime")

router = APIRouter()


def _resolve(bucket: str, session_id: str):
    path = projects.find_session(bucket, session_id)
    if not path:
        raise HTTPException(404, "session not found")
    meta = projects.session_meta(path)
    return path, meta


@router.get("/api/runtime/{bucket}/{session_id}/state")
async def runtime_state(bucket: str, session_id: str) -> dict:
    path, meta = _resolve(bucket, session_id)
    try:
        state = await controller.get_state(
            session_id, path, remote_name=meta.remote_name
        )
    except zellij.ZellijError as e:
        raise HTTPException(502, f"zellij error: {e}")
    return state.to_dict()


class ControlBody(BaseModel):
    allow_takeover: bool = False


@router.post("/api/runtime/{bucket}/{session_id}/control")
async def take_control(bucket: str, session_id: str, body: ControlBody) -> dict:
    path, meta = _resolve(bucket, session_id)
    try:
        state = await controller.ensure_managed(
            session_id,
            path,
            meta.cwd,
            allow_takeover=body.allow_takeover,
            remote_name=meta.remote_name,
        )
    except TakeoverConfirmationRequired as e:
        # 409 + flag: the UI turns this into an explicit confirmation step.
        raise HTTPException(
            409, detail={"needs_takeover_confirmation": True, "reason": str(e)}
        )
    except ControlRefused as e:
        raise HTTPException(409, detail={"reason": str(e)})
    except zellij.ZellijError as e:
        raise HTTPException(502, f"zellij error: {e}")
    return state.to_dict()


class PendingBody(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


@router.get("/api/runtime/{bucket}/{session_id}/pending")
def list_pending(bucket: str, session_id: str) -> dict:
    return {"pending": db.pending_prompt_list(bucket, session_id)}


@router.post("/api/runtime/{bucket}/{session_id}/pending")
def create_pending(bucket: str, session_id: str, body: PendingBody) -> dict:
    _resolve(bucket, session_id)  # 404 on unknown session
    return db.pending_prompt_add(bucket, session_id, body.text)


@router.patch("/api/runtime/pending/{prompt_id}")
def edit_pending(prompt_id: int, body: PendingBody) -> dict:
    if not db.pending_prompt_update_text(prompt_id, body.text):
        raise HTTPException(409, "prompt is not editable (already sent or gone)")
    return db.pending_prompt_get(prompt_id)  # type: ignore[return-value]


@router.delete("/api/runtime/pending/{prompt_id}")
def delete_pending(prompt_id: int) -> dict:
    if not db.pending_prompt_delete(prompt_id):
        raise HTTPException(404, "pending prompt not found")
    return {"ok": True}


@router.post("/api/runtime/{bucket}/{session_id}/pending/{prompt_id}/send")
async def send_pending(bucket: str, session_id: str, prompt_id: int) -> dict:
    _resolve(bucket, session_id)
    row = db.pending_prompt_get(prompt_id)
    if not row or row["session_id"] != session_id:
        raise HTTPException(404, "pending prompt not found")

    # Atomic claim — the double-send guard.
    if not db.pending_prompt_claim(prompt_id):
        raise HTTPException(409, "prompt already sent (or being sent)")

    try:
        await controller.send_prompt(session_id, row["text"])
    except (ControlRefused, zellij.ZellijError) as e:
        db.pending_prompt_finish(prompt_id, ok=False)
        raise HTTPException(409, detail={"reason": str(e)})
    except Exception:
        db.pending_prompt_finish(prompt_id, ok=False)
        raise
    db.pending_prompt_finish(prompt_id, ok=True)
    _log.info("prompt %s delivered to session %s", prompt_id, session_id)
    return {"ok": True, "id": prompt_id}


@router.post("/api/runtime/{bucket}/{session_id}/interrupt")
async def interrupt(bucket: str, session_id: str) -> dict:
    _resolve(bucket, session_id)
    try:
        await controller.interrupt(session_id)
    except (ControlRefused, zellij.ZellijError) as e:
        raise HTTPException(409, detail={"reason": str(e)})
    return {"ok": True}


class ModeBody(BaseModel):
    mode: str = Field(pattern=r"^(manual|accept_edits|plan|bypass)$")


@router.post("/api/runtime/{bucket}/{session_id}/mode")
async def set_mode(bucket: str, session_id: str, body: ModeBody) -> dict:
    """Change the managed TUI's permission mode (Shift+Tab cycle)."""
    _resolve(bucket, session_id)
    try:
        mode = await controller.set_mode(session_id, body.mode)
    except (ControlRefused, zellij.ZellijError) as e:
        raise HTTPException(409, detail={"reason": str(e)})
    return {"ok": True, "mode": mode}


class RespondBody(BaseModel):
    choice: str = Field(pattern=r"^(\d{1,2}|esc)$")


@router.post("/api/runtime/{bucket}/{session_id}/respond")
async def respond(bucket: str, session_id: str, body: RespondBody) -> dict:
    """Answer a blocking TUI dialog (permission prompt etc.) in the managed
    pane. `choice` is the option number shown in the dialog, or "esc"."""
    _resolve(bucket, session_id)
    try:
        await controller.respond(session_id, body.choice)
    except (ControlRefused, zellij.ZellijError) as e:
        raise HTTPException(409, detail={"reason": str(e)})
    return {"ok": True}
