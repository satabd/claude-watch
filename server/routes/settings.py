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
        "available_models": providers.AVAILABLE_MODELS,
        "tiers": providers.TIERS,
        # What each tier will actually use right now: the saved override if
        # it is still offered, otherwise the built-in default.
        "models": {
            p_name: {
                t["key"]: providers.resolve(
                    p_name,
                    t["key"],
                    override=db.get_setting(f"model_{p_name}_{t['key']}", None),
                )[1]
                for t in providers.TIERS
            }
            for p_name in providers.PROVIDERS
        },
    }


class UpdateSettings(BaseModel):
    provider: str | None = None
    # {"claude": {"fast": "sonnet"}} — set a tier's model. Pass null to clear
    # an override and fall back to the built-in default.
    models: dict[str, dict[str, str | None]] | None = None


@router.post("/api/settings")
def update_settings(req: UpdateSettings) -> dict:
    if req.provider is not None:
        if req.provider not in providers.PROVIDERS:
            raise HTTPException(400, f"unknown provider: {req.provider}")
        db.set_setting("provider", req.provider)
    if req.models is not None:
        valid_tiers = {t["key"] for t in providers.TIERS}
        for p_name, tiers in req.models.items():
            if p_name not in providers.PROVIDERS:
                raise HTTPException(400, f"unknown provider: {p_name}")
            for tier, model in tiers.items():
                if tier not in valid_tiers:
                    raise HTTPException(400, f"unknown tier: {tier}")
                key = f"model_{p_name}_{tier}"
                if model is None:
                    db.set_setting(key, "")  # empty → falls back to default
                    continue
                if not providers.is_valid_model(p_name, model):
                    raise HTTPException(
                        400, f"{p_name} does not offer model: {model}"
                    )
                db.set_setting(key, model)
    return get_settings()
