"""Phase 7: scorecards, hire-an-agent, retro, Playbook A/B."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal, require_role
from app.db import get_db
from app.models import Actor, AgentProfile
from app.schemas import ConfirmHireRequest, HireOut, HireRequest
from app.services import intelligence
from app.tenancy import Tenant

router = APIRouter(tags=["intelligence"])


@router.get("/scorecards")
def scorecards(db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> list[dict]:
    return intelligence.snapshot_all(db, p.org_id)


@router.get("/scorecards/{actor_id}")
def actor_scorecard(actor_id: str, db: Session = Depends(get_db),
                    p: Principal = Depends(current_principal)) -> dict:
    actor = Tenant(db, p.org_id).get(Actor, actor_id)
    if actor is None:
        raise HTTPException(status_code=404, detail="actor not found")
    return intelligence.scorecard(db, p.org_id, actor)


@router.post("/hire", response_model=HireOut)
def hire(body: HireRequest, db: Session = Depends(get_db),
         p: Principal = Depends(require_role("ceo"))) -> HireOut:
    """Generate a draft profile from a job description and eval it before hiring."""
    profile = intelligence.generate_profile(db, p.org_id, body.job_description)
    result = intelligence.run_eval(db, p.org_id, profile)
    db.commit()
    return HireOut(profile_id=profile.id, name=profile.name, eval=result)


@router.post("/hire/{profile_id}/confirm")
def confirm(profile_id: str, body: ConfirmHireRequest, db: Session = Depends(get_db),
            p: Principal = Depends(require_role("ceo"))) -> dict:
    profile = Tenant(db, p.org_id).get(AgentProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    actor = intelligence.confirm_hire(db, p.org_id, profile, body.department_id)
    db.commit()
    return {"actor_id": actor.id, "hired": True}


@router.post("/retro")
def retro(db: Session = Depends(get_db), p: Principal = Depends(require_role("ceo", "dept_head"))) -> dict:
    result = intelligence.retro(db, p.org_id)
    db.commit()
    return result


@router.get("/playbooks/ab")
def playbook_ab(department_id: str, a: int, b: int, db: Session = Depends(get_db),
                p: Principal = Depends(current_principal)) -> dict:
    return intelligence.ab_compare(db, p.org_id, department_id, a, b)
