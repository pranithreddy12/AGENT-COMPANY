"""Lead drafts a DAG -> approve schedules it -> execute lands artifacts -> slip recomputes."""
from sqlalchemy import select

from app.models import Actor, Artifact, Task
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import planning


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def test_draft_routes_to_department_not_lead(db):
    org_id = _org(db)
    project, tasks = planning.draft_project(db, org_id, "Build a widget")
    db.commit()

    assert len(tasks) == 7  # cross-department DAG
    lead = db.scalars(select(Actor).where(Actor.role == "lead")).first()
    # the Lead assigns work to department agents, never itself
    assert all(t.assignee_actor_id != lead.id for t in tasks)
    assert all(t.department_id is not None for t in tasks)
    assert all(t.status == "proposed" for t in tasks)
    # spans at least four departments
    assert len({t.department_id for t in tasks}) >= 4


def test_approve_then_execute_lands_artifacts(db):
    org_id = _org(db)
    project, tasks = planning.draft_project(db, org_id, "Build a widget")
    db.commit()

    summary = planning.approve_project(db, project)
    assert summary["critical_path"]  # non-empty
    assert project.status == "active" and project.health == "on_track"
    assert project.due_at is not None

    arts = planning.execute_project(db, project)
    assert len(arts) == 7
    assert all(a.content for a in arts)
    assert all(t.status == "done" for t in db.scalars(select(Task).where(Task.project_id == project.id)))


def test_slip_marks_project_slipping(db):
    org_id = _org(db)
    project, tasks = planning.draft_project(db, org_id, "Build a widget")
    db.commit()
    planning.approve_project(db, project)
    before = project.due_at

    # slip a critical task (technical spec) -> project finish must move later
    spec = next(t for t in db.scalars(select(Task).where(Task.project_id == project.id)) if "technical spec" in t.goal)
    planning.slip_task(db, project, spec, added_hours=4.0)

    assert project.due_at > before
    assert project.health == "slipping"
