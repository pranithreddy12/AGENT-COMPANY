"""Inbound integrations. LeadForge posts here when a prospect is ready for delivery."""
import secrets as pysecrets
import threading

from fastapi import APIRouter, Depends, HTTPException
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


@router.post("/integrations/leadforge/proposal")
def leadforge_proposal(body: LeadForgeHandoff, db: Session = Depends(get_db),
                       p: Principal = Depends(leadforge_principal)) -> dict:
    """Kick off ONE client-ready proposal for a prospect and return immediately with a proposal_id.
    Generation (research + LLM + Legal, up to ~60s) runs in the background; the webhook never blocks
    and never returns the draft text. LeadForge fetches the text later via GET /proposals/{id}, which
    only releases it once a human has approved it. Idempotent on leadforge_lead_id (a retry returns
    the same proposal_id, not a new proposal)."""
    project, is_new = integrations.start_proposal(db, p.org_id, body)
    project_id, status = project.id, project.status
    db.commit()  # persist the shell before the worker (its own session) loads it
    if is_new:
        threading.Thread(target=integrations.run_proposal_in_background,
                         args=(project_id, body), daemon=True).start()
    return {"proposal_id": project_id, "status": status, "idempotent": not is_new}


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str, db: Session = Depends(get_db),
                 p: Principal = Depends(leadforge_principal)) -> dict:
    """Fetch a proposal by id (LeadForge via secret, or a ceo/dept_head via Bearer). Returns status
    only while generating / awaiting approval; releases the proposal TEXT only once a human has
    approved it and it isn't Legal-blocked. This is the send gate — LeadForge can't get sendable
    text until a human clears it."""
    view = integrations.proposal_view(db, p.org_id, proposal_id)
    if view is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    return view


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: str, db: Session = Depends(get_db),
                     p: Principal = Depends(require_role("ceo", "dept_head"))) -> dict:
    """Human-only: approve a generated proposal so its text can be fetched and sent. No webhook-secret
    path — a machine can't self-approve. Refuses a proposal still generating (409) or Legal-blocked
    (409; override the veto first)."""
    result = integrations.approve_proposal(db, p.org_id, proposal_id)
    if result is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    if result.get("error") == "not_ready":
        raise HTTPException(status_code=409, detail="proposal not generated yet")
    if result.get("error") == "blocked":
        raise HTTPException(status_code=409, detail=f"Legal veto in place — override it first: {result.get('block_reason')}")
    db.commit()
    return result
