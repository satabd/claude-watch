"""Remote SSH host management — CRUD, test, sync."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from dataclasses import asdict as _asdict

from .. import db, remotes, wsl


async def _reload_watchers() -> None:
    """Ask the remote-watcher manager to re-read DB state and (re)start tasks.

    Awaitable so the route handler can await it. We don't fire-and-forget:
    the caller usually expects the watcher state to reflect the change before
    returning to the user (so e.g. a subsequent `Test` call doesn't race).
    """
    try:
        from ..main import remote_watcher_manager  # local import: avoid cycle

        await remote_watcher_manager.reload()
    except Exception as e:  # noqa: BLE001
        # Don't propagate manager errors back to the API caller; log only.
        import logging

        logging.getLogger("watcher.routes").warning("reload failed: %s", e)

router = APIRouter()


class HostCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(22, ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=64)
    key_path: str | None = None
    projects_path: str | None = None


class HostUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    key_path: str | None = None
    projects_path: str | None = None
    enabled: bool | None = None


@router.get("/api/remotes")
def list_remotes() -> dict:
    return {"items": db.list_remote_hosts()}


@router.post("/api/remotes/discover-wsl")
async def discover_wsl() -> dict:
    """Enumerate installed WSL distros and probe each for SSH readiness.

    Returns suggestions the UI can one-click into the Add-host form.
    """
    distros = await wsl.discover_distros()
    return {
        "items": [_asdict(d) for d in distros],
        # Convenience for the UI: build the recommended SSH host body for any
        # ready-to-go distro. We use 127.0.0.1 (not "localhost") because
        # WSL2's localhost forwarding is IPv4-only, and asyncssh prefers IPv6
        # when given a hostname.
        "suggestions": [
            {
                "name": f"wsl-{d.name.lower()}",
                "host": "127.0.0.1",
                "port": d.ssh_port,
                "username": d.user,
                "key_path": None,
                "projects_path": d.suggested_projects_path,
            }
            for d in distros
            if d.ssh_running and d.user
        ],
    }


@router.post("/api/remotes")
async def create_remote(req: HostCreate) -> dict:
    existing = db.get_remote_host_by_name(req.name)
    if existing:
        raise HTTPException(409, f"remote '{req.name}' already exists")
    out = db.add_remote_host(
        name=req.name,
        host=req.host,
        port=req.port,
        username=req.username,
        key_path=req.key_path,
        projects_path=req.projects_path,
    )
    await _reload_watchers()
    return out


@router.patch("/api/remotes/{host_id}")
async def update_remote(host_id: int, req: HostUpdate) -> dict:
    fields = req.model_dump(exclude_unset=True, exclude_none=False)
    if "enabled" in fields and fields["enabled"] is not None:
        fields["enabled"] = 1 if fields["enabled"] else 0
    h = db.update_remote_host(host_id, **fields)
    if not h:
        raise HTTPException(404, "remote not found")
    await _reload_watchers()
    return h


@router.delete("/api/remotes/{host_id}")
async def delete_remote(host_id: int) -> dict:
    h = db.get_remote_host(host_id)
    if not h:
        raise HTTPException(404, "remote not found")
    # Cancel the watcher first so it can't write any more bytes into the dir
    # we're about to delete.
    db.delete_remote_host(host_id)
    await _reload_watchers()
    removed = remotes.remove_host_mirror(h["name"])
    return {"ok": True, "files_removed": removed}


@router.post("/api/remotes/{host_id}/test")
async def test_remote(host_id: int) -> dict:
    h = db.get_remote_host(host_id)
    if not h:
        raise HTTPException(404, "remote not found")
    result = await remotes.test_connection(h)
    if result.get("ok"):
        db.update_remote_host(
            host_id,
            home_dir=result.get("home_dir"),
            platform=result.get("platform"),
            last_error=None,
        )
    else:
        db.update_remote_host(host_id, last_error=result.get("error"))
    return result


@router.post("/api/remotes/{host_id}/sync")
async def sync_remote(host_id: int) -> dict:
    h = db.get_remote_host(host_id)
    if not h:
        raise HTTPException(404, "remote not found")
    if not h.get("enabled"):
        raise HTTPException(400, "remote is disabled")
    report = await remotes.sync_host(h)
    import time as _t

    db.update_remote_host(
        host_id,
        last_synced_ms=int(_t.time() * 1000),
        last_error=report.error if not report.ok else None,
        home_dir=report.home_dir,
        platform=report.platform,
    )
    return asdict(report)
