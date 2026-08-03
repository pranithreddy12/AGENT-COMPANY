"""Phase 2 gate: cross-department project completes, every cross-dept edge = a HandoffPacket,
Critic loop is bounded, Legal veto is human-override-only."""
from sqlalchemy import select

from app.models import Actor, Department, HandoffPacket, Task
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import planning, review


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def test_multidept_project_completes_with_handoffs(db):
    org_id = _org(db)
    project, tasks = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    planning.approve_project(db, project)
    arts = planning.execute_project(db, project)

    assert project.status == "done"
    assert len(arts) == 7

    # a HandoffPacket exists for every cross-department dependency edge
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.project_id == project.id))}
    expected_edges = sum(
        1 for t in tasks.values() for d in t.depends_on
        if tasks[d].department_id and t.department_id and tasks[d].department_id != t.department_id
    )
    packets = list(db.scalars(select(HandoffPacket).where(HandoffPacket.project_id == project.id)))
    assert len(packets) == expected_edges > 0


def test_critic_cap_escalates(db):
    # a task the Critic can never pass must stop after the cap and flag for a human, not loop
    org_id = _org(db)
    project, _ = planning.draft_project(db, org_id, "x")
    db.commit()
    planning.approve_project(db, project)
    task = db.scalars(select(Task).where(Task.project_id == project.id)).first()
    task.acceptance_criteria = "IMPOSSIBLE"  # deterministic force-revise hook
    db.flush()

    critic = db.scalars(select(Actor).where(Actor.role == "critic")).first()
    art = planning._run_and_review(db, project, task, critic)
    assert art.needs_human is True
    assert art.version == planning.MAX_REVISE_CYCLES + 1  # bounded: exactly cap+1 attempts


def test_legal_veto_blocks_and_only_human_overrides(db):
    import pytest
    from fastapi import HTTPException

    from app.auth import Principal, require_role
    from app.models import Artifact, Task
    from app.routers.projects import override_veto

    # unit: prohibited content is blocked; clean content passes
    assert review.legal_review("we offer guaranteed returns").passed is False
    assert review.legal_review("a normal status update").passed is True

    org_id = _org(db)
    project, _ = planning.draft_project(db, org_id, "x")
    db.commit()
    task = db.scalars(select(Task).where(Task.project_id == project.id)).first()
    art = Artifact(org_id=org_id, task_id=task.id, content="PROHIBITED claim", blocked=True,
                   block_reason="prohibited content")
    db.add(art)
    db.commit()

    # a non-privileged human cannot reach the override
    with pytest.raises(HTTPException) as exc:
        require_role("ceo", "dept_head")(Principal("u", org_id, "member"))
    assert exc.value.status_code == 403

    # a human with the role clears the veto
    out = override_veto(art.id, db=db, p=Principal("u", org_id, "ceo"))
    assert out.blocked is False and out.status == "approved"
