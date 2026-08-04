"""Phase 7: scorecards, hire-an-agent, retro, Playbook A/B, + the agent roster & activity feed."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal, require_role
from app.db import get_db
from app.models import Actor, AgentProfile, AgentRun, Department, Task
from app.schemas import AskAgentRequest, ConfirmHireRequest, HireOut, HireRequest
from app.services import intelligence, talk
from app.tenancy import Tenant

router = APIRouter(tags=["intelligence"])


@router.get("/agents")
def list_agents(db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> list[dict]:
    """The workforce: every agent with its role, department, model, scorecard, and its own memory
    of tasks — what it's completed and what's still on its plate."""
    depts = {d.id: d.name for d in db.scalars(select(Department).where(Department.org_id == p.org_id))}
    tasks_by_actor: dict[str, list] = {}
    for t in db.scalars(select(Task).where(Task.org_id == p.org_id)):
        tasks_by_actor.setdefault(t.assignee_actor_id, []).append(t)
    out = []
    for a in db.scalars(select(Actor).where(Actor.org_id == p.org_id, Actor.type == "agent")):
        prof = db.get(AgentProfile, a.agent_profile_id) if a.agent_profile_id else None
        mine = tasks_by_actor.get(a.id, [])
        out.append({
            "id": a.id, "name": a.name or (prof.name if prof else "?"), "role": a.role,
            "department": depts.get(a.department_id) or ("—" if a.role != "lead" else "org-wide"),
            "provider": prof.provider if prof else "?", "model": prof.model if prof else "?",
            "autonomy": prof.autonomy_default if prof else "?", "status": a.status,
            "scorecard": intelligence.scorecard(db, p.org_id, a),
            "todo": [t.goal for t in mine if t.status != "done"],
            "completed": [t.goal for t in mine if t.status == "done"],
        })
    role_order = {"lead": 0, "member": 1, "critic": 2}
    return sorted(out, key=lambda x: (role_order.get(x["role"], 3), x["department"]))


@router.get("/activity")
def activity(limit: int = 40, db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> list[dict]:
    """Recent agent runs, newest first — what each agent actually did."""
    depts = {d.id: d.name for d in db.scalars(select(Department).where(Department.org_id == p.org_id))}
    actors = {a.id: a for a in db.scalars(select(Actor).where(Actor.org_id == p.org_id))}
    runs = db.scalars(select(AgentRun).where(AgentRun.org_id == p.org_id)
                      .order_by(AgentRun.started_at.desc().nullslast()).limit(min(limit, 100)))
    out = []
    for r in runs:
        a = actors.get(r.actor_id)
        out.append({
            "id": r.id, "agent": (a.name if a and a.name else (a.role if a else "?")), "role": a.role if a else "?",
            "department": (depts.get(a.department_id) if a and a.department_id else ("org-wide" if a and a.role == "lead" else "—")),
            "trigger": (r.trigger or "")[:70], "status": r.status, "turns": r.turns_used,
            "cost_usd": r.cost_usd, "started_at": r.started_at.isoformat() if r.started_at else None,
        })
    return out


@router.post("/agents/{agent_id}/ask")
def ask_agent(agent_id: str, body: AskAgentRequest, db: Session = Depends(get_db),
              p: Principal = Depends(current_principal)) -> dict:
    """Ask an agent anything — a status update, a question about its work, anything."""
    actor = Tenant(db, p.org_id).get(Actor, agent_id)
    if actor is None or actor.type != "agent":
        raise HTTPException(status_code=404, detail="agent not found")
    answer = talk.ask_agent(db, p.org_id, actor, body.question, body.project_id)
    return {"agent": actor.name or actor.role, "answer": answer}


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
