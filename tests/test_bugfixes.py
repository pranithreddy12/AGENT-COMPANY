"""Regression tests for the three bugs found in the /ultrareview-substitute pass."""
import pytest
from sqlalchemy import select

from app.auth import Principal
from app.models import Actor, Artifact, Task
from app.routers import human
from app.routers.orgs import create_org
from app.schemas import AssignRequest, OrgCreate
from app.services import planning


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


# Bug 1: a Legal-blocked artifact must keep the project out of "done".
def test_legal_block_stops_completion(db):
    org_id = _org(db)
    project, _ = planning.draft_project(db, org_id, "Deliver engagement")
    db.commit()
    planning.approve_project(db, project)
    # make the Sales task emit prohibited content so the Legal task blocks its artifact
    sales_task = next(t for t in db.scalars(select(Task).where(Task.project_id == project.id))
                      if "Qualify" in t.goal)
    sales_task.goal = sales_task.goal + " with guaranteed returns"
    db.flush()

    arts = planning.execute_project(db, project)
    assert any(a.blocked for a in arts)              # Legal actually blocked something
    assert project.status != "done"                  # ...and that stops completion (was the bug)


# Bug 2: a Critic exception escalates to a human instead of crashing execution.
def test_critic_error_fails_closed(db, monkeypatch):
    org_id = _org(db)
    project, _ = planning.draft_project(db, org_id, "Deliver engagement")
    db.commit()
    planning.approve_project(db, project)

    def boom(*a, **k):
        raise RuntimeError("critic API down")

    monkeypatch.setattr(planning, "_review", boom)
    arts = planning.execute_project(db, project)      # must NOT raise
    assert all(a.needs_human for a in arts)
    assert project.status != "done"


# Bug 3: assign rejects an actor id that isn't in the caller's org.
def test_assign_rejects_unknown_actor(db):
    from fastapi import HTTPException
    org_id = _org(db)
    project, _ = planning.draft_project(db, org_id, "Deliver engagement")
    db.commit()
    task = db.scalars(select(Task).where(Task.project_id == project.id)).first()
    ceo = Principal("ceo", org_id, "ceo")

    with pytest.raises(HTTPException) as exc:
        human.assign(task.id, AssignRequest(assignee_actor_id="does-not-exist"), db=db, p=ceo)
    assert exc.value.status_code == 404

    # a real actor in the org is accepted
    real = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.role == "member")).first()
    out = human.assign(task.id, AssignRequest(reviewer_actor_id=real.id), db=db, p=ceo)
    assert out["reviewer"] == real.id
