"""Review Threads endpoints.

Forward-focused conversational review of Claude Code work with another LLM
(Codex in V1). Threads, messages, and an audit snapshot of the evidence
that was sent each time live in SQLite. Provider continuity uses the
underlying CLI's resume mechanism (Codex session id from stderr); on
failure we fall back to a fresh provider session and replace the stored
id, never replaying old messages.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db, projects as projects_module
from ..git_capture import capture as git_capture_fn
from ..providers import REVIEW_PROVIDERS
from ..providers.codex_provider import CodexResumeFailed
from ..review_packet import (
    PacketEvidence,
    PacketInputs,
    REVIEWER_MODES,
    build_packet,
)

_log = logging.getLogger("watcher.routes.reviews")

router = APIRouter(prefix="/api/reviews")


# ---------------------------------------------------------------------------
# Pydantic types — mirror the SQLite shape but expose JSON for audit fields.
# ---------------------------------------------------------------------------


class ThreadOut(BaseModel):
    id: int
    name: str
    provider: str
    project_bucket: str | None
    claude_session_id: str | None
    provider_session_id: str | None
    created_at: int
    updated_at: int
    archived_at: int | None


class MessageOut(BaseModel):
    id: int
    thread_id: int
    role: str
    content: str
    source_session_id: str | None
    source_turn_uuid: str | None
    context_used_json: dict | None
    evidence_used_json: dict | None
    provider: str | None
    model: str | None
    estimated_tokens: int | None
    provider_tokens: int | None
    created_at: int


class CreateThreadIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    project_bucket: str | None = None
    claude_session_id: str | None = None
    provider: str = "codex"


class PatchThreadIn(BaseModel):
    name: str | None = None
    archived: bool | None = None


class PacketEvidenceIn(BaseModel):
    include_claude_turn: bool = True
    include_git_status: bool = True
    include_changed_files: bool = True
    include_git_diff: bool = True
    include_test_output: bool = True
    include_build_output: bool = True


ReviewerMode = Literal["critical", "prompt_coach"]


class PreviewIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=20_000)
    reviewer_mode: ReviewerMode = "critical"
    project_bucket: str | None = None
    project_cwd: str | None = None
    claude_session_id: str | None = None
    claude_turn_uuid: str | None = None
    claude_turn_role: str | None = None
    claude_turn_text: str | None = None
    test_output: str | None = None
    build_output: str | None = None
    evidence: PacketEvidenceIn = Field(default_factory=PacketEvidenceIn)


class GitSummary(BaseModel):
    is_repo: bool
    branch: str | None = None
    ahead: int = 0
    behind: int = 0
    dirty_count: int = 0
    diff_byte_count: int = 0
    diff_truncated: bool = False


class SecretHitOut(BaseModel):
    label: str
    location: str


class PreviewOut(BaseModel):
    byte_count: int
    estimated_tokens: int
    git: GitSummary
    secret_hits: list[SecretHitOut]
    prompt_preview: str  # first ~1 KB so the UI can show what will be sent


class SendIn(PreviewIn):
    thread_id: int
    secret_override: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_thread(row: dict) -> ThreadOut:
    return ThreadOut(**{k: row[k] for k in ThreadOut.model_fields})


def _row_to_message(row: dict) -> MessageOut:
    return MessageOut(
        id=row["id"],
        thread_id=row["thread_id"],
        role=row["role"],
        content=row["content"],
        source_session_id=row["source_session_id"],
        source_turn_uuid=row["source_turn_uuid"],
        context_used_json=json.loads(row["context_used_json"])
        if row["context_used_json"]
        else None,
        evidence_used_json=json.loads(row["evidence_used_json"])
        if row["evidence_used_json"]
        else None,
        provider=row["provider"],
        model=row["model"],
        estimated_tokens=row["estimated_tokens"],
        provider_tokens=row["provider_tokens"],
        created_at=row["created_at"],
    )


def _resolve_cwd_from_bucket(bucket: str | None) -> Path | None:
    """Best-effort: find the project's cwd by scanning the projects list.
    Returns None if the bucket is unknown / has no associated cwd."""
    if not bucket:
        return None
    for p in projects_module.list_projects():
        if p.bucket == bucket and p.cwd:
            return Path(p.cwd)
        # Sessions may carry their own bucket (different from project.bucket)
        # after the merge-by-cwd step on the frontend. Match those too.
        for s in p.sessions:
            if s.bucket == bucket and (s.cwd or p.cwd):
                return Path(s.cwd or p.cwd)
    return None


async def _build_packet_from_request(req: PreviewIn, secret_override: bool):
    """Resolve the cwd, capture git, build the packet. Shared between
    /preview and /send so they always see the same evidence shape."""
    if req.reviewer_mode not in REVIEWER_MODES:
        raise HTTPException(400, f"unknown reviewer_mode: {req.reviewer_mode}")

    # Prefer an explicit project_cwd from the client (matches what the user
    # sees in the UI); fall back to deriving from the bucket.
    cwd: Path | None
    if req.project_cwd:
        cwd = Path(req.project_cwd)
    else:
        cwd = _resolve_cwd_from_bucket(req.project_bucket)

    git = await git_capture_fn(cwd) if cwd else None

    inputs = PacketInputs(
        question=req.question,
        reviewer_mode=req.reviewer_mode,
        project_cwd=str(cwd) if cwd else None,
        claude_session_id=req.claude_session_id,
        claude_turn_uuid=req.claude_turn_uuid,
        claude_turn_role=req.claude_turn_role,
        claude_turn_text=req.claude_turn_text,
        test_output=req.test_output,
        build_output=req.build_output,
        evidence=PacketEvidence(**req.evidence.model_dump()),
    )
    packet = build_packet(inputs, git, secret_override=secret_override)
    return packet, git


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/threads", response_model=ThreadOut)
def create_thread(req: CreateThreadIn) -> ThreadOut:
    if req.provider not in REVIEW_PROVIDERS:
        raise HTTPException(400, f"unsupported review provider: {req.provider}")
    row = db.create_review_thread(
        name=req.name,
        provider=req.provider,
        project_bucket=req.project_bucket,
        claude_session_id=req.claude_session_id,
    )
    return _row_to_thread(row)


@router.get("/threads", response_model=list[ThreadOut])
def list_threads(project_bucket: str | None = None) -> list[ThreadOut]:
    rows = db.list_review_threads(project_bucket=project_bucket)
    return [_row_to_thread(r) for r in rows]


@router.patch("/threads/{thread_id}", response_model=ThreadOut)
def patch_thread(thread_id: int, req: PatchThreadIn) -> ThreadOut:
    if not db.get_review_thread(thread_id):
        raise HTTPException(404, "thread not found")
    row = db.update_review_thread(
        thread_id, name=req.name, archived=req.archived
    )
    if row is None:
        raise HTTPException(404, "thread not found")
    return _row_to_thread(row)


@router.get(
    "/threads/{thread_id}/messages", response_model=list[MessageOut]
)
def list_messages(thread_id: int) -> list[MessageOut]:
    if not db.get_review_thread(thread_id):
        raise HTTPException(404, "thread not found")
    rows = db.list_review_messages(thread_id)
    return [_row_to_message(r) for r in rows]


@router.post("/preview", response_model=PreviewOut)
async def preview(req: PreviewIn) -> PreviewOut:
    packet, git = await _build_packet_from_request(req, secret_override=False)
    return PreviewOut(
        byte_count=packet.byte_count,
        estimated_tokens=packet.estimated_tokens,
        git=GitSummary(
            is_repo=bool(git and git.is_repo),
            branch=git.branch if git else None,
            ahead=git.ahead if git else 0,
            behind=git.behind if git else 0,
            dirty_count=len(git.dirty) if git and git.is_repo else 0,
            diff_byte_count=git.diff_byte_count if git else 0,
            diff_truncated=git.diff_truncated if git else False,
        ),
        secret_hits=[
            SecretHitOut(label=h.label, location=h.location)
            for h in packet.secret_hits
        ],
        prompt_preview=packet.prompt[:1024],
    )


@router.post("/send", response_model=MessageOut)
async def send(req: SendIn) -> MessageOut:
    thread = db.get_review_thread(req.thread_id)
    if not thread:
        raise HTTPException(404, "thread not found")

    provider_name = thread["provider"]
    provider_fn = REVIEW_PROVIDERS.get(provider_name)
    if provider_fn is None:
        raise HTTPException(
            400, f"thread provider {provider_name!r} is not registered"
        )

    packet, _git = await _build_packet_from_request(
        req, secret_override=req.secret_override
    )

    if packet.secret_hits and not req.secret_override:
        # 409 = the request was well-formed but we refuse to send. UI
        # surfaces the hits and asks the user to override or remove the
        # offending evidence item.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "SECRET_DETECTED",
                "hits": [
                    {"label": h.label, "location": h.location}
                    for h in packet.secret_hits
                ],
            },
        )

    # Persist the user message FIRST so the audit row exists even if the
    # provider call fails. source_turn_uuid is set here when the review was
    # opened from a specific Claude turn — that anchors the thread to the
    # actual work and lets us answer "which Claude turn was this about?"
    # later from a single SQL query.
    user_msg = db.add_review_message(
        thread_id=req.thread_id,
        role="user",
        content=req.question,
        source_session_id=req.claude_session_id,
        source_turn_uuid=req.claude_turn_uuid,
        context_used_json=json.dumps(
            {
                "reviewer_mode": req.reviewer_mode,
                "evidence_toggles": req.evidence.model_dump(),
            }
        ),
        evidence_used_json=json.dumps(packet.audit_snapshot),
        provider=provider_name,
        estimated_tokens=packet.estimated_tokens,
    )

    # Try resume first if we have a session id; on failure, drop it and
    # cold-start. We DO NOT replay old messages — the provider sees only
    # this packet.
    session_id_in = thread["provider_session_id"]
    result = None
    resume_attempted = bool(session_id_in)
    resume_succeeded = False
    if session_id_in:
        try:
            result = await provider_fn(
                packet.prompt, session_id_in=session_id_in
            )
            resume_succeeded = True
        except CodexResumeFailed as e:
            _log.warning(
                "codex resume failed for thread %d (will retry cold): %s",
                req.thread_id,
                e,
            )
    if result is None:
        try:
            result = await provider_fn(packet.prompt, session_id_in=None)
        except Exception as e:
            # We already persisted the user message; surface a clear error
            # so the UI can show it inline.
            _log.exception("review provider call failed")
            raise HTTPException(502, f"provider call failed: {e}") from e

    # TODO: Promote `resume_attempted` / `resume_succeeded` to dedicated
    # columns on review_messages so dashboards / audits can answer "how
    # often does Codex resume actually work?" without having to JSON-decode
    # every context_used_json blob. For now they live inside the JSON; this
    # comment marks the upgrade path for a future migration v5.
    reviewer_msg = db.add_review_message(
        thread_id=req.thread_id,
        role="reviewer",
        content=result.text,
        source_session_id=req.claude_session_id,
        source_turn_uuid=req.claude_turn_uuid,
        context_used_json=json.dumps(
            {
                "reviewer_mode": req.reviewer_mode,
                "resume_attempted": resume_attempted,
                "resume_succeeded": resume_succeeded,
            }
        ),
        evidence_used_json=None,
        provider=provider_name,
        model=result.model,
        provider_tokens=result.tokens_used,
    )
    # Replace provider_session_id with whatever the provider returned this
    # time (usually unchanged on resume; new id when we cold-started).
    db.update_review_thread(
        req.thread_id, provider_session_id=result.session_id_out
    )
    return _row_to_message(reviewer_msg)
