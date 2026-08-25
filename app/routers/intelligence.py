"""Phase 7: scorecards, hire-an-agent, retro, Playbook A/B, + the agent roster & activity feed."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from datetime import timedelta

from app.auth import Principal, current_principal, require_role
from app.db import get_db
from app.models import Actor, AgentProfile, AgentRun, Artifact, Department, Project, Task
from app.schemas import AgentProfileUpdate, AskAgentRequest, ConfirmHireRequest, HireOut, HireRequest
from app.services import intelligence, talk
from app.tenancy import Tenant

router = APIRouter(tags=["intelligence"])

VALID_AUTONOMY = {"L0", "L1", "L2", "L3"}


def _task_history(db: Session, org_id: str, actor_id: str, tasks: list[Task]) -> dict:
    """Bucket an agent's tasks into completed / in_progress / scheduled / blocked, each with the
    project it belongs to and a real timestamp — not just a flat list of goal strings."""
    proj_ids = {t.project_id for t in tasks}
    projects = {pr.id: pr for pr in db.scalars(select(Project).where(Project.id.in_(proj_ids)))} if proj_ids else {}
    # latest artifact per task -> a real "completed at" instead of guessing
    arts = db.scalars(select(Artifact).where(Artifact.task_id.in_([t.id for t in tasks]))
                      .order_by(Artifact.created_at.desc())) if tasks else []
    latest_art_by_task: dict[str, Artifact] = {}
    for art in arts:
        latest_art_by_task.setdefault(art.task_id, art)  # first hit per task_id is the newest (already sorted)

    def entry(t: Task) -> dict:
        pr = projects.get(t.project_id)
        art = latest_art_by_task.get(t.id)
        when = None
        if t.status == "done" and art:
            when = art.created_at.isoformat()
        elif t.status in ("proposed", "scheduled") and t.due_at:
            when = t.due_at.isoformat()
        elif t.status in ("proposed", "scheduled") and pr and t.est_start_h:
            when = (pr.start_at + timedelta(hours=t.est_start_h)).isoformat()
        return {"id": t.id, "goal": t.goal, "project_id": t.project_id,
                "project_goal": pr.goal if pr else None, "status": t.status, "when": when}

    return {
        "completed": [entry(t) for t in tasks if t.status == "done"],
        "in_progress": [entry(t) for t in tasks if t.status in ("running", "in_progress")],
        "scheduled": [entry(t) for t in tasks if t.status in ("proposed", "scheduled")],
        "blocked": [entry(t) for t in tasks if t.status == "blocked"],
    }


@router.get("/agents")
def list_agents(db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> list[dict]:
    """The workforce: every agent with its role, department, model, scorecard, its editable config,
    and the real history of its work — completed, in progress, and scheduled for later."""
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
            "profile_id": prof.id if prof else None,
            "system_prompt": prof.system_prompt if prof else "",
            "max_turns": prof.max_turns if prof else None,
            "max_tokens": prof.max_tokens if prof else None,
            "cost_ceiling_usd": prof.cost_ceiling_usd if prof else None,
            "scorecard": intelligence.scorecard(db, p.org_id, a),
            "history": _task_history(db, p.org_id, a.id, mine),
            # kept for any older client code reading these — same data the new "history" buckets carry
            "todo": [t.goal for t in mine if t.status != "done"],
            "completed": [t.goal for t in mine if t.status == "done"],
        })
    role_order = {"lead": 0, "member": 1, "critic": 2}
    return sorted(out, key=lambda x: (role_order.get(x["role"], 3), x["department"]))


@router.patch("/agents/{agent_id}")
def update_agent(agent_id: str, body: AgentProfileUpdate, db: Session = Depends(get_db),
                 p: Principal = Depends(require_role("ceo", "dept_head"))) -> dict:
    """Edit an agent's own configuration — system prompt, autonomy, budget. Provider/model stay
    owned by the org-wide Model setting (Governance) so the two controls never fight each other:
    saving there re-points every agent, which would silently clobber a per-agent override here."""
    actor = Tenant(db, p.org_id).get(Actor, agent_id)
    if actor is None or actor.type != "agent":
        raise HTTPException(status_code=404, detail="agent not found")
    prof = db.get(AgentProfile, actor.agent_profile_id) if actor.agent_profile_id else None
    if prof is None:
        raise HTTPException(status_code=404, detail="agent has no profile to edit")
    if body.autonomy_default is not None and body.autonomy_default not in VALID_AUTONOMY:
        raise HTTPException(status_code=422, detail=f"autonomy_default must be one of {sorted(VALID_AUTONOMY)}")
    if body.max_turns is not None and body.max_turns < 1:
        raise HTTPException(status_code=422, detail="max_turns must be >= 1")
    if body.max_tokens is not None and body.max_tokens < 1:
        raise HTTPException(status_code=422, detail="max_tokens must be >= 1")
    if body.cost_ceiling_usd is not None and body.cost_ceiling_usd < 0:
        raise HTTPException(status_code=422, detail="cost_ceiling_usd must be >= 0")
    if body.system_prompt is not None:
        prof.system_prompt = body.system_prompt
    if body.autonomy_default is not None:
        prof.autonomy_default = body.autonomy_default
    if body.max_turns is not None:
        prof.max_turns = body.max_turns
    if body.max_tokens is not None:
        prof.max_tokens = body.max_tokens
    if body.cost_ceiling_usd is not None:
        prof.cost_ceiling_usd = body.cost_ceiling_usd
    db.commit()
    return {"id": actor.id, "system_prompt": prof.system_prompt, "autonomy": prof.autonomy_default,
            "max_turns": prof.max_turns, "max_tokens": prof.max_tokens, "cost_ceiling_usd": prof.cost_ceiling_usd}


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
