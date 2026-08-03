"""Phase 7 intelligence: scorecards, hire-an-agent, retro, Playbook A/B. All measurement is code;
models write and judge, they don't score themselves.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Actor, AgentProfile, AgentRun, Artifact, Department, Playbook, Project, Scorecard, Task,
)
from app.services import playbooks, review, runs


def _rate(n: int, d: int) -> float:
    return round(n / d, 3) if d else 0.0


# ---- scorecards ----

def scorecard(db: Session, org_id: str, actor: Actor) -> dict:
    tasks = list(db.scalars(select(Task).where(Task.org_id == org_id, Task.assignee_actor_id == actor.id)))
    arts = list(db.scalars(select(Artifact).where(Artifact.org_id == org_id, Artifact.produced_by_actor_id == actor.id)))
    runs_ = list(db.scalars(select(AgentRun).where(AgentRun.org_id == org_id, AgentRun.actor_id == actor.id)))
    done = sum(1 for t in tasks if t.status == "done")
    cost = sum(r.cost_usd for r in runs_)
    return {
        "actor_id": actor.id,
        "tasks_assigned": len(tasks),
        "tasks_completed": done,
        "completion_rate": _rate(done, len(tasks)),
        "rework_rate": _rate(sum(1 for a in arts if a.version > 1), len(arts)),
        "first_pass_rate": _rate(sum(1 for a in arts if a.version == 1 and a.status in ("reviewed", "approved")), len(arts)),
        "escalation_rate": _rate(sum(1 for a in arts if a.needs_human), len(arts)),
        "blocked_rate": _rate(sum(1 for a in arts if a.blocked), len(arts)),
        "cost_per_task": round(cost / done, 6) if done else 0.0,
        "runs": len(runs_),
    }


def snapshot_all(db: Session, org_id: str) -> list[dict]:
    cards = []
    for actor in db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.type == "agent")):
        m = scorecard(db, org_id, actor)
        db.add(Scorecard(org_id=org_id, actor_id=actor.id, period="all", metrics=m))
        cards.append(m)
    db.flush()
    return cards


def best_agent(db: Session, org_id: str, department_id: str) -> Actor | None:
    """Auto-routing: pick the department agent with the best (first_pass, completion) record."""
    agents = list(db.scalars(select(Actor).where(
        Actor.org_id == org_id, Actor.department_id == department_id, Actor.type == "agent", Actor.role == "member")))
    if not agents:
        return None
    return max(agents, key=lambda a: (lambda m: (m["first_pass_rate"], m["completion_rate"]))(scorecard(db, org_id, a)))


# ---- hire-an-agent ----

_EVAL_TASKS = ["Draft a one-paragraph project brief", "Summarize the acceptance criteria"]


def generate_profile(db: Session, org_id: str, job_description: str) -> AgentProfile:
    """Turn a job description into a draft AgentProfile (not yet an Actor). Deterministic here;
    an LLM would author the prompt/tool grants behind the same seam."""
    name = " ".join(job_description.split()[:4]).title() or "New Agent"
    profile = AgentProfile(
        org_id=org_id, name=name, system_prompt=job_description, provider="echo", model="echo-1",
        max_turns=4, autonomy_default="L1", tool_grants=["echo", "get_time"],
    )
    db.add(profile)
    db.flush()
    return profile


def run_eval(db: Session, org_id: str, profile: AgentProfile) -> dict:
    """Run the draft profile against a small eval set before hiring. Reports pass/fail."""
    tmp = Actor(org_id=org_id, type="agent", role="member", agent_profile_id=profile.id, status="candidate")
    db.add(tmp)
    db.flush()
    passed = 0
    for goal in _EVAL_TASKS:
        run = runs.execute(db, runs.create_run(db, org_id, tmp, goal))
        content = (run.result or {}).get("text", "") if run.status == "succeeded" else ""
        if review.critic_review(content, "non-empty deliverable", "").passed:
            passed += 1
    db.delete(tmp)  # candidate actor removed; hire creates the real one on confirm
    db.flush()
    return {"ran": len(_EVAL_TASKS), "passed": passed}


def confirm_hire(db: Session, org_id: str, profile: AgentProfile, department_id: str | None) -> Actor:
    actor = Actor(org_id=org_id, type="agent", role="member", agent_profile_id=profile.id,
                  department_id=department_id, status="active")
    db.add(actor)
    db.flush()
    return actor


# ---- retro ----

def retro(db: Session, org_id: str) -> dict:
    """Scan recent work for recurring failure modes and propose Playbook amendments (drafts)."""
    arts = list(db.scalars(select(Artifact).where(Artifact.org_id == org_id)))
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.org_id == org_id))}
    findings, proposals = [], []

    # group problems by department
    by_dept: dict[str, dict] = {}
    for a in arts:
        task = tasks.get(a.task_id)
        dept_id = task.department_id if task else None
        if not dept_id:
            continue
        d = by_dept.setdefault(dept_id, {"rework": 0, "escalations": 0, "blocked": 0, "n": 0})
        d["n"] += 1
        d["rework"] += 1 if a.version > 1 else 0
        d["escalations"] += 1 if a.needs_human else 0
        d["blocked"] += 1 if a.blocked else 0

    for dept_id, d in by_dept.items():
        dept = db.get(Department, dept_id)
        issues = []
        if d["escalations"]:
            issues.append(f"{d['escalations']} escalation(s)")
        if d["rework"]:
            issues.append(f"{d['rework']} rework cycle(s)")
        if d["blocked"]:
            issues.append(f"{d['blocked']} Legal block(s)")
        if not issues:
            continue
        findings.append({"department": dept.name, "issues": issues})
        rule = "Add an explicit acceptance-criteria checklist and a compliance pass before submitting."
        amendment = playbooks.amend(db, org_id, dept_id, rule,
                                    change_summary=f"Retro: {', '.join(issues)}")
        proposals.append({"department": dept.name, "amendment_playbook_id": amendment.id, "proposed_rule": rule})

    failed_runs = db.scalar(select(func.count(AgentRun.id)).where(
        AgentRun.org_id == org_id, AgentRun.status == "failed")) or 0
    return {"findings": findings, "proposed_amendments": proposals, "failed_runs": failed_runs}


# ---- Playbook A/B ----

def ab_compare(db: Session, org_id: str, department_id: str, version_a: int, version_b: int) -> dict:
    tasks = {t.id for t in db.scalars(select(Task).where(Task.department_id == department_id))}
    arts = [a for a in db.scalars(select(Artifact).where(Artifact.org_id == org_id)) if a.task_id in tasks]

    def stats(v: int) -> dict:
        group = [a for a in arts if a.playbook_version == v]
        n = len(group)
        return {"version": v, "artifacts": n,
                "first_pass_rate": _rate(sum(1 for a in group if a.version == 1 and a.status in ("reviewed", "approved")), n),
                "rework_rate": _rate(sum(1 for a in group if a.version > 1), n)}

    return {"a": stats(version_a), "b": stats(version_b)}
