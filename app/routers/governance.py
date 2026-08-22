"""Phase 3 gate: outbound send -> blocked/queued -> CEO decision -> rejection changes next draft.
Plus kill switch, department pause, approval queue."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal, require_role
from app.db import get_db
from app.models import Actor, ApprovalRequest, Department, Organization
from app.schemas import ApprovalOut, DecideRequest, OutboundRequest
from app.services import governance
from app.tenancy import Tenant

router = APIRouter(tags=["governance"])


def _org(db: Session, p: Principal) -> Organization:
    org = db.get(Organization, p.org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    return org


@router.post("/outbound")
def outbound(body: OutboundRequest, db: Session = Depends(get_db),
             p: Principal = Depends(current_principal)) -> dict:
    actor = Tenant(db, p.org_id).get(Actor, body.actor_id)
    if actor is None:
        raise HTTPException(status_code=404, detail="actor not found")
    try:
        return governance.request_outbound(db, _org(db, p), actor, body.action_type, body.intent)
    except governance.Denied as e:
        raise HTTPException(status_code=403, detail={"reason": e.reason, "rule": e.rule})


@router.get("/approvals", response_model=list[ApprovalOut])
def approvals(status: str = "pending", db: Session = Depends(get_db),
              p: Principal = Depends(current_principal)) -> list[ApprovalOut]:
    q = select(ApprovalRequest).where(ApprovalRequest.org_id == p.org_id, ApprovalRequest.status == status)
    return [
        ApprovalOut(id=a.id, action_type=a.action_type, preview=a.preview, status=a.status,
                    requested_by_actor_id=a.requested_by_actor_id, department_id=a.department_id,
                    created_at=a.created_at, decision_reason=a.decision_reason)
        for a in db.scalars(q)
    ]


@router.post("/approvals/{approval_id}/decide")
def decide(approval_id: str, body: DecideRequest, db: Session = Depends(get_db),
           p: Principal = Depends(require_role("ceo", "dept_head"))) -> dict:
    ar = Tenant(db, p.org_id).get(ApprovalRequest, approval_id)
    if ar is None:
        raise HTTPException(status_code=404, detail="approval not found")
    if body.decision not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="decision must be approve|reject")
    try:
        return governance.decide(db, _org(db, p), ar, p.user_id, body.decision, body.reason)
    except governance.Denied as e:
        raise HTTPException(status_code=409, detail=e.reason)


@router.post("/governance/kill")
def kill(db: Session = Depends(get_db), p: Principal = Depends(require_role("ceo"))) -> dict:
    org = _org(db, p)
    org.killed = True
    db.commit()
    return {"killed": True}


@router.post("/governance/resume")
def resume(db: Session = Depends(get_db), p: Principal = Depends(require_role("ceo"))) -> dict:
    org = _org(db, p)
    org.killed = False
    db.commit()
    return {"killed": False}


@router.post("/governance/simulation")
def set_simulation(on: bool = True, db: Session = Depends(get_db),
                   p: Principal = Depends(require_role("ceo"))) -> dict:
    org = _org(db, p)
    org.simulation = on
    db.commit()
    return {"simulation": on}


@router.post("/departments/{dept_id}/pause")
def pause_dept(dept_id: str, paused: bool = True, db: Session = Depends(get_db),
               p: Principal = Depends(require_role("ceo", "dept_head"))) -> dict:
    dept = Tenant(db, p.org_id).get(Department, dept_id)
    if dept is None:
        raise HTTPException(status_code=404, detail="department not found")
    dept.paused = paused
    db.commit()
    return {"department_id": dept_id, "paused": paused}
