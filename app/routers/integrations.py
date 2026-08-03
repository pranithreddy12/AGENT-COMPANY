"""Inbound integrations. LeadForge posts here when a prospect is ready for delivery."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import Principal, require_role
from app.db import get_db
from app.routers.projects import _task_out
from app.schemas import LeadForgeHandoff, LeadForgeHandoffResult
from app.services import integrations

router = APIRouter(tags=["integrations"])


@router.post("/integrations/leadforge/handoff", response_model=LeadForgeHandoffResult)
def leadforge_handoff(body: LeadForgeHandoff, db: Session = Depends(get_db),
                      p: Principal = Depends(require_role("ceo", "dept_head"))) -> LeadForgeHandoffResult:
    """LeadForge -> Company OS: a warm reply / proposal request becomes an Account + a decomposed
    delivery Project. Auth: Bearer token for a service user in the org (LeadForge holds one).
    # ponytail: reuses JWT + tenant scoping; add a long-lived webhook secret for production SaaS.
    """
    account, lead, project, tasks = integrations.ingest_handoff(db, p.org_id, body)
    db.commit()
    return LeadForgeHandoffResult(
        account_id=account.id, lead_id=lead.id, project_id=project.id,
        project_status=project.status, tasks=[_task_out(t) for t in tasks],
    )
