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

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db, projects
from ..runtime import zellij
from ..runtime.controller import (
    ControlRefused,
    controller,
    parse_blocking_dialog,
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
            session_id,
            path,
            remote_name=meta.remote_name,
            cwd=meta.cwd,
            title=meta.ai_title,
        )
    except zellij.ZellijError as e:
        raise HTTPException(502, f"zellij error: {e}")
    return state.to_dict()


class ControlBody(BaseModel):
    """No fields. There is nothing to confirm: claude-watch either can resume
    this session (nothing else is alive on it) or it refuses. Kept as a body
    so the endpoint stays a POST with room to grow."""


@router.post("/api/runtime/{bucket}/{session_id}/control")
async def take_control(bucket: str, session_id: str, body: ControlBody) -> dict:
    """Resume a session into a managed pane. Never takes one from a running
    claude — see CLAUDE.md, "Ownership"."""
    path, meta = _resolve(bucket, session_id)
    try:
        state = await controller.ensure_managed(
            session_id,
            path,
            meta.cwd,
            remote_name=meta.remote_name,
            title=meta.ai_title,
        )
    except ControlRefused as e:
        raise HTTPException(409, detail={"reason": str(e)})
    except zellij.ZellijError as e:
        raise HTTPException(502, f"zellij error: {e}")
    return state.to_dict()


class NewSessionBody(BaseModel):
    """`prompt` is the first thing to say to the new session.

    It matters more than it looks: Claude writes no transcript until a session
    has a turn in it, and the sidebar is built from transcripts — so a session
    created with no prompt exists, and is managed, but cannot be selected
    until someone types into its pane. Sending the first prompt here is what
    makes the session appear.
    """

    title: str | None = None
    prompt: str | None = Field(default=None, max_length=100_000)


@router.post("/api/runtime/{bucket}/sessions")
async def new_session(bucket: str, body: NewSessionBody) -> dict:
    """Start a brand-new Claude session in this project, managed from birth.

    This is the front door: claude-watch picks the session id, so the session
    is owned from its first breath instead of being discovered later as an
    anonymous external process.
    """
    cwd = projects.project_cwd(bucket)
    if not cwd:
        raise HTTPException(
            400,
            detail={
                "reason": "Cannot work out a directory for this project. "
                "Remote projects are view-only, and a local one needs a "
                "transcript that still points at a directory that exists."
            },
        )
    try:
        session_id, state = await controller.create_session(cwd, title=body.title)
    except ControlRefused as e:
        raise HTTPException(409, detail={"reason": str(e)})
    except zellij.ZellijError as e:
        raise HTTPException(502, f"zellij error: {e}")

    # The sidebar is built from transcripts on disk, so a session nobody can
    # see yet is not much use. Claude writes the file at startup; give it a
    # moment so the caller can select the session straight away.
    # Give the session its first turn, which is also what materialises the
    # transcript the viewer needs.
    prompt = (body.prompt or "").strip()
    if prompt:
        try:
            await controller.send_prompt(session_id, prompt)
        except (ControlRefused, zellij.ZellijError) as e:
            # The pane is up and owned; only the prompt failed. Say so rather
            # than pretending the whole thing did.
            _log.warning("new session %s: first prompt failed: %s", session_id, e)

    ready = False
    for _ in range(20):
        if projects.find_session(bucket, session_id):
            ready = True
            break
        await asyncio.sleep(0.25)

    # No transcript yet usually means Claude is sitting on its first-run trust
    # prompt, which it shows before opening a session in a directory it has
    # not seen approved. Say so — the session is unselectable until it writes,
    # so "nothing happened" would otherwise be the whole user experience.
    blocked = None
    if not ready and state.zellij_session and state.pane_id:
        try:
            screen = await zellij.dump_screen(state.zellij_session, state.pane_id)
            blocked = parse_blocking_dialog(screen)
        except zellij.ZellijError:
            pass
    _log.info(
        "new session %s in %s (transcript_ready=%s, blocked=%s)",
        session_id, bucket, ready, bool(blocked),
    )
    return {
        "session_id": session_id,
        "bucket": bucket,
        "cwd": cwd,
        "transcript_ready": ready,
        "blocked_on": blocked,
        "attach_command": state.to_dict().get("attach_command"),
        "state": state.to_dict(),
    }


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


@router.post("/api/runtime/{bucket}/{session_id}/release")
async def release(bucket: str, session_id: str) -> dict:
    """Close the managed pane and verify its claude actually exited."""
    _resolve(bucket, session_id)
    try:
        return await controller.release(session_id)
    except (ControlRefused, zellij.ZellijError) as e:
        raise HTTPException(409, detail={"reason": str(e)})


@router.post("/api/runtime/{bucket}/{session_id}/interrupt")
async def interrupt(bucket: str, session_id: str) -> dict:
    _resolve(bucket, session_id)
    try:
        await controller.interrupt(session_id)
    except (ControlRefused, zellij.ZellijError) as e:
        raise HTTPException(409, detail={"reason": str(e)})
    return {"ok": True}


class ModeBody(BaseModel):
    mode: str = Field(pattern=r"^(manual|accept_edits|plan|auto|dont_ask|bypass)$")


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
