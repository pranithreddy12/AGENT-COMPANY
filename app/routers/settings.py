"""Org-level LLM "brain" settings — pick a provider/model + key from the console instead of
editing .env and restarting the process."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal, require_role
from app.config import settings
from app.db import get_db
from app.models import Organization
from app.schemas import LLMSettingsIn
from app.services import llm

router = APIRouter(tags=["settings"])

VALID_PROVIDERS = {"echo", "ollama", "mistral", "openrouter", "anthropic"}


@router.get("/settings/llm")
def get_llm_settings(db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> dict:
    """Current org-level model config. Never returns the raw key — only whether one is set — same
    pattern as the LeadForge webhook secret."""
    org = db.get(Organization, p.org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    # "has_key" mirrors exactly what build_provider would actually use (org key OR env fallback) —
    # never just "is org.llm_api_keys non-empty", which would lie about providers with only an env key.
    has_key = bool(org.llm_provider) and bool(llm.resolve_api_key(db, p.org_id, org.llm_provider))
    return {
        "provider": org.llm_provider, "model": org.llm_model, "has_key": has_key,
        "providers": sorted(VALID_PROVIDERS),
        "env_configured": {name: bool(getattr(settings, attr, None)) for name, attr in llm._ENV_KEY_ATTR.items()},
    }


@router.post("/settings/llm")
def set_llm_settings(body: LLMSettingsIn, db: Session = Depends(get_db),
                     p: Principal = Depends(require_role("ceo"))) -> dict:
    """Point every agent in the org at this provider/model, and store the key (if given) so it
    works right away. Fails closed on an unknown provider or a keyed provider with no key anywhere
    (neither this request nor .env) — never silently accepts a config that can't actually run."""
    if body.provider not in VALID_PROVIDERS:
        raise HTTPException(status_code=422, detail=f"unknown provider {body.provider!r}")
    if not body.model.strip():
        raise HTTPException(status_code=422, detail="model is required")
    llm.configure_org_llm(db, p.org_id, body.provider, body.model.strip(), body.api_key)
    db.flush()
    try:
        llm.build_provider(body.provider, body.model.strip(), llm.resolve_api_key(db, p.org_id, body.provider))
    except RuntimeError as e:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(e))
    db.commit()
    return {"provider": body.provider, "model": body.model.strip(), "saved": True}
