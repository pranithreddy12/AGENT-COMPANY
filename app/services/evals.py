"""Eval harness: runs the REAL code paths (planning, playbooks, critic) and scores them.

Deterministic on EchoProvider (so the harness itself is tested without a key); flips to the real
Anthropic model via use_anthropic() for the keyed validation run. This is the substrate for
"is agent quality good / rising" — the thing that turns green tests into a claim about intelligence.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Actor, AgentProfile, Artifact, Department, Task
from app.services import planning, playbooks, scheduling


def use_anthropic(db: Session, org_id: str, model: str = "claude-sonnet-5") -> None:
    """Point every agent (Lead, workers, Critic) at the real provider. The --live switch."""
    for prof in db.scalars(select(AgentProfile).where(AgentProfile.org_id == org_id)):
        prof.provider, prof.model = "anthropic", model
    db.commit()


def lead_decomposition_eval(db: Session, org_id: str, goal: str = "Launch a new client onboarding service") -> dict:
    """Does the Lead turn a novel goal into a sane, acyclic, multi-step DAG?"""
    project, tasks = planning.draft_project(db, org_id, goal)
    db.commit()
    nodes = [{"id": t.id, "effort": t.est_effort_hours, "deps": list(t.depends_on)} for t in tasks]
    try:
        scheduling.topo_order(nodes)
        acyclic = True
    except scheduling.CycleError:
        acyclic = False
    depts = len({t.department_id for t in tasks})
    passed = acyclic and len(tasks) >= 3 and depts >= 1 and all(t.goal.strip() for t in tasks)
    return {"name": "lead_decomposition", "passed": passed,
            "tasks": len(tasks), "departments": depts, "acyclic": acyclic}


def sop_behavior_eval(db: Session, org_id: str,
                      rule: str = "Every deliverable must include a confidentiality note") -> dict:
    """The signature test: does editing the Playbook (not the prompt) change agent output?"""
    project, _ = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    planning.approve_project(db, project)
    planning.execute_project(db, project)

    dev = db.scalars(select(Department).where(Department.org_id == org_id, Department.name == "Development")).first()
    task = db.scalars(select(Task).where(Task.project_id == project.id, Task.department_id == dev.id)).first()
    before = db.scalars(select(Artifact).where(Artifact.task_id == task.id)).first().content

    amendment = playbooks.amend(db, org_id, dev.id, rule, change_summary="eval")
    playbooks.activate(db, amendment)
    db.commit()
    after = planning.rerun_task(db, project, db.get(Task, task.id)).content

    # heuristic signal: the rule's intent shows up after activation and the output actually changed
    changed = after != before and ("confidential" in after.lower() or rule.lower()[:20] in after.lower())
    return {"name": "sop_behavior", "passed": changed, "before": before[:80], "after": after[:160]}


def critic_eval(db: Session, org_id: str) -> dict:
    """Does the Critic pass a good artifact and reject an empty one?"""
    good = planning.judge(db, org_id, "A complete project brief covering every acceptance criterion.",
                          "A one-paragraph brief that covers the acceptance criteria", "")
    bad = planning.judge(db, org_id, "", "A one-paragraph brief that covers the acceptance criteria", "")
    passed = good.passed and not bad.passed
    return {"name": "critic", "passed": passed, "good_passed": good.passed, "bad_passed": bad.passed}


def run_all(db: Session, org_id: str) -> dict:
    results = [lead_decomposition_eval(db, org_id), sop_behavior_eval(db, org_id), critic_eval(db, org_id)]
    return {"passed": sum(r["passed"] for r in results), "total": len(results), "results": results}
