"""Shared project memory: agents write what they produced and read the team's context before working."""
from sqlalchemy import select

from app.models import Artifact, Department, MemoryRecord, Task
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import planning


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def test_memory_accumulates_and_context_flows(db):
    org_id = _org(db)
    project, _ = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    planning.approve_project(db, project)
    planning.execute_project(db, project)

    # every completed task left a memory record for the team
    mem = list(db.scalars(select(MemoryRecord).where(MemoryRecord.project_id == project.id)))
    assert len(mem) >= 5
    assert any("Sales completed" in m.content for m in mem)

    # a downstream agent's context includes its upstream dependency's real deliverable + shared memory
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.project_id == project.id))}
    depts = {d.id: d for d in db.scalars(select(Department).where(Department.org_id == org_id))}
    arts = {a.task_id: a for a in db.scalars(select(Artifact).where(Artifact.task_id.in_(list(tasks))))}
    spec = next(t for t in tasks.values() if "technical spec" in t.goal)  # depends on the Sales task

    ctx = planning._gather_context(db, project, spec, tasks, arts, depts)
    assert "Sales" in ctx                          # sees the upstream Sales deliverable, not a blank slate
    assert "Shared project knowledge" in ctx       # and the accumulated team memory


def test_context_reaches_the_agent_prompt(db, monkeypatch):
    # the gathered context is actually passed into the run the agent sees (not dropped)
    org_id = _org(db)
    project, _ = planning.draft_project(db, org_id, "Deliver engagement")
    db.commit()
    planning.approve_project(db, project)

    seen = []
    real = planning.runs.execute
    def spy(db_, run, extra_system=""):
        seen.append(run.trigger)
        return real(db_, run, extra_system=extra_system)
    monkeypatch.setattr(planning.runs, "execute", spy)
    planning.execute_project(db, project)

    # at least one downstream task's prompt carried the team-context preamble
    assert any("Context from your team" in t for t in seen)
