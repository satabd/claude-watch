"""Settings endpoints — currently just provider selection + cache stats."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, providers

router = APIRouter()

DEFAULT_PROVIDER = "claude"


def get_provider() -> str:
    return db.get_setting("provider", DEFAULT_PROVIDER) or DEFAULT_PROVIDER


@router.get("/api/settings")
def get_settings() -> dict:
    return {
        "provider": get_provider(),
        "available_providers": sorted(providers.PROVIDERS.keys()),
        "default_models": providers.DEFAULT_MODELS,
    }


class UpdateSettings(BaseModel):
    provider: str | None = None


@router.post("/api/settings")
def update_settings(req: UpdateSettings) -> dict:
    if req.provider is not None:
        if req.provider not in providers.PROVIDERS:
            raise HTTPException(400, f"unknown provider: {req.provider}")
        db.set_setting("provider", req.provider)
    return get_settings()
