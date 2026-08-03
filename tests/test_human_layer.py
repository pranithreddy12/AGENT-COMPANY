"""Phase 4 gate: human joins -> assigned -> reviews an artifact -> correction changes future
agent behavior via the Playbook (not a prompt)."""
from sqlalchemy import select

from app.auth import Principal
from app.models import Actor, Artifact, Department, Playbook, Task
from app.routers import human
from app.routers.orgs import create_org
from app.schemas import AnnotateRequest, AssignRequest, TeamMemberCreate
from app.services import planning, playbooks


def _setup(db):
    org_id = create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id
    project, _ = planning.draft_project(db, org_id, "Deliver engagement")
    db.commit()
    planning.approve_project(db, project)
    planning.execute_project(db, project)
    return org_id, project


from app.schemas import OrgCreate  # noqa: E402


def _ceo(org_id):
    return Principal("ceo-user", org_id, "ceo")


def test_playbook_amendment_changes_agent_behavior(db):
    org_id, project = _setup(db)
    ceo = _ceo(org_id)

    # a Development task + its artifact
    dev = db.scalars(select(Department).where(Department.org_id == org_id, Department.name == "Development")).first()
    task = db.scalars(select(Task).where(Task.project_id == project.id, Task.department_id == dev.id)).first()
    art = db.scalars(select(Artifact).where(Artifact.task_id == task.id)).first()
    assert "Applied SOP" not in art.content  # v1 playbook has no RULE

    # a human joins Development
    tm = human.add_team_member(
        TeamMemberCreate(email="dev@acme.com", password="pw", department_id=dev.id, role="member"),
        db=db, p=ceo)
    # assigned as reviewer on the task (paired: agent drafts, human reviews)
    human.assign(task.id, AssignRequest(reviewer_actor_id=tm.actor_id), db=db, p=ceo)
    assert db.get(Task, task.id).reviewer_actor_id == tm.actor_id

    # human reviews the artifact and proposes a correction -> drafts a Playbook amendment
    human_principal = Principal(tm.user_id, org_id, "member")
    res = human.annotate(art.id, AnnotateRequest(text="Specs must state a confidentiality note",
                                                 proposed_rule="Include a confidentiality note"),
                         db=db, p=human_principal)
    amendment_id = res["amendment_playbook_id"]
    assert amendment_id

    # amendment is a draft, not yet active -> re-run still lacks the rule
    art2 = planning.rerun_task(db, project, db.get(Task, task.id))
    assert "Applied SOP" not in art2.content

    # activate the amendment (human control), then re-run -> behavior changed via the Playbook
    human.activate(amendment_id, db=db, p=ceo)
    art3 = planning.rerun_task(db, project, db.get(Task, task.id))
    assert "Include a confidentiality note" in art3.content

    # exactly one active playbook for the department; prior superseded
    actives = list(db.scalars(select(Playbook).where(
        Playbook.department_id == dev.id, Playbook.status == "active")))
    assert len(actives) == 1 and actives[0].version == 2


def test_standup_digest_counts(db):
    from app.routers.console import standup
    org_id, project = _setup(db)
    dig = standup(db=db, p=_ceo(org_id))
    assert dig["shipped"]["tasks_done"] >= 1
    assert "pending_approvals" in dig["needs_you"]
    assert dig["cost"]["cap_usd"] > 0
