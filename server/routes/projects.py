"""REST endpoints for project + session listing and content."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from .. import parser, projects

router = APIRouter()


@router.get("/api/projects")
def get_projects() -> dict:
    items = projects.list_projects()
    return {
        "projects": [
            {
                "bucket": p.bucket,
                "display_name": p.display_name,
                "cwd": p.cwd,
                "last_modified_ms": p.last_modified_ms,
                "session_count": len(p.sessions),
                "sessions": [asdict(s) for s in p.sessions],
            }
            for p in items
        ]
    }


@router.get("/api/sessions/{bucket}/{session_id}")
def get_session(bucket: str, session_id: str) -> dict:
    path = projects.find_session(bucket, session_id)
    if not path:
        raise HTTPException(404, "session not found")
    meta = projects.session_meta(path)
    events = parser.parse_file(path)
    return {
        "meta": asdict(meta),
        "events": [e.to_dict() for e in events],
    }


@router.get("/api/sessions/{bucket}/{session_id}/raw")
def get_session_raw(bucket: str, session_id: str) -> dict:
    path = projects.find_session(bucket, session_id)
    if not path:
        raise HTTPException(404, "session not found")
    text = path.read_text(encoding="utf-8", errors="replace")
    return {"content": text, "size": len(text.encode("utf-8"))}
