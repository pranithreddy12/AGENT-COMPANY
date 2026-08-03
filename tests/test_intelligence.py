"""Phase 7: scorecards, hire-an-agent, retro, A/B, and failure-injection hardening."""
from sqlalchemy import select

from app.models import Actor, AgentProfile, Department, Task
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import intelligence, planning


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def _run_project(db, org_id, goal="Deliver engagement"):
    project, _ = planning.draft_project(db, org_id, goal)
    db.commit()
    planning.approve_project(db, project)
    planning.execute_project(db, project)
    return project


def test_scorecards_compute(db):
    org_id = _org(db)
    _run_project(db, org_id)
    cards = intelligence.snapshot_all(db, org_id)
    # at least one worker agent completed tasks with a first-pass artifact
    workers = [c for c in cards if c["tasks_completed"] > 0]
    assert workers
    w = workers[0]
    assert 0.0 <= w["first_pass_rate"] <= 1.0
    assert w["completion_rate"] == 1.0  # echo happy path completes


def test_hire_flow(db):
    org_id = _org(db)
    dev = db.scalars(select(Department).where(Department.name == "Development")).first()
    profile = intelligence.generate_profile(db, org_id, "Senior backend engineer for API work")
    result = intelligence.run_eval(db, org_id, profile)
    assert result["ran"] == 2 and result["passed"] == 2  # echo passes the eval set
    actor = intelligence.confirm_hire(db, org_id, profile, dev.id)
    assert actor.type == "agent" and actor.department_id == dev.id
    # the hired agent actually runs
    from app.services import runs
    run = runs.execute(db, runs.create_run(db, org_id, actor, "do a thing"))
    assert run.status == "succeeded"


def test_retro_proposes_amendment_for_failure_mode(db):
    org_id = _org(db)
    project = _run_project(db, org_id)
    # inject a failure mode: mark an artifact as escalated/needs_human
    from app.models import Artifact
    art = db.scalars(select(Artifact).where(Artifact.org_id == org_id)).first()
    art.needs_human = True
    db.flush()

    result = intelligence.retro(db, org_id)
    assert result["findings"]  # found the escalation
    assert result["proposed_amendments"]  # proposed a Playbook amendment (draft)


def test_ab_compare(db):
    org_id = _org(db)
    project = _run_project(db, org_id)
    dev = db.scalars(select(Department).where(Department.name == "Development")).first()
    cmp = intelligence.ab_compare(db, org_id, dev.id, 1, 2)
    assert cmp["a"]["version"] == 1 and "first_pass_rate" in cmp["a"]


def test_failure_injection_fails_closed(db, monkeypatch):
    """A provider that raises mid-run must fail the task closed — no crash, project not 'done'."""
    org_id = _org(db)
    project, _ = planning.draft_project(db, org_id, "Deliver engagement")
    db.commit()
    planning.approve_project(db, project)

    from app.services import runs as runs_mod

    def boom(*a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(runs_mod, "build_provider", boom)
    arts = planning.execute_project(db, project)  # must not raise

    tasks = list(db.scalars(select(Task).where(Task.project_id == project.id)))
    assert all(t.status == "blocked" for t in tasks)  # every task failed closed
    assert project.status != "done"
