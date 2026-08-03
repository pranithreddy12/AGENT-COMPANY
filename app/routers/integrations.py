"""Inbound integrations. LeadForge posts here when a prospect is ready for delivery."""
import secrets as pysecrets

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import Principal, hash_secret, leadforge_principal, require_role
from app.db import get_db
from app.models import Organization
from app.routers.projects import _task_out
from app.schemas import LeadForgeHandoff, LeadForgeHandoffResult
from app.services import integrations

router = APIRouter(tags=["integrations"])


@router.post("/integrations/leadforge/secret")
def rotate_secret(db: Session = Depends(get_db), p: Principal = Depends(require_role("ceo"))) -> dict:
    """Generate (or rotate) the long-lived LeadForge webhook secret. Shown once; only its hash
    is stored. Put it in LeadForge as X-LeadForge-Secret."""
    org = db.get(Organization, p.org_id)
    raw = pysecrets.token_urlsafe(32)
    org.webhook_secret_hash = hash_secret(raw)
    db.commit()
    return {"secret": raw, "note": "store in LeadForge as X-LeadForge-Secret; shown once, not recoverable"}


@router.post("/integrations/leadforge/handoff", response_model=LeadForgeHandoffResult)
def leadforge_handoff(body: LeadForgeHandoff, db: Session = Depends(get_db),
                      p: Principal = Depends(leadforge_principal)) -> LeadForgeHandoffResult:
    """LeadForge -> Company OS: a warm reply / proposal request becomes an Account + a decomposed
    delivery Project. Auth: X-LeadForge-Secret (long-lived) or a ceo/dept_head Bearer token."""
    account, lead, project, tasks = integrations.ingest_handoff(db, p.org_id, body)
    db.commit()
    return LeadForgeHandoffResult(
        account_id=account.id, lead_id=lead.id, project_id=project.id,
        project_status=project.status, tasks=[_task_out(t) for t in tasks],
    )
