"""Phase 4 human layer: humans as Actors, paired tasks, review -> Playbook amendment."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal, hash_password, issue_token, require_role
from app.db import get_db
from app.models import Actor, Annotation, Artifact, Playbook, Project, Task, User
from app.schemas import (
    AnnotateRequest, AssignRequest, PlaybookOut, TeamMemberCreate, TeamMemberOut,
)
from app.services import playbooks
from app.tenancy import Tenant

router = APIRouter(tags=["human"])


@router.post("/team", response_model=TeamMemberOut)
def add_team_member(body: TeamMemberCreate, db: Session = Depends(get_db),
                    p: Principal = Depends(require_role("ceo"))) -> TeamMemberOut:
    """A human joins a department: create a User + a human Actor bound to it."""
    from app.models import Department
    if Tenant(db, p.org_id).get(Department, body.department_id) is None:
        raise HTTPException(status_code=404, detail="department not found")
    user = User(org_id=p.org_id, email=body.email, pw_hash=hash_password(body.password), role=body.role)
    db.add(user)
    db.flush()
    actor = Actor(org_id=p.org_id, type="human", role=body.role, user_id=user.id,
                  department_id=body.department_id)
    db.add(actor)
    db.commit()
    return TeamMemberOut(user_id=user.id, actor_id=actor.id, access_token=issue_token(user))


@router.post("/tasks/{task_id}/assign")
def assign(task_id: str, body: AssignRequest, db: Session = Depends(get_db),
           p: Principal = Depends(require_role("ceo", "dept_head"))) -> dict:
    tenant = Tenant(db, p.org_id)
    task = tenant.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if body.assignee_actor_id is not None:
        if tenant.get(Actor, body.assignee_actor_id) is None:  # no cross-tenant / dangling refs
            raise HTTPException(status_code=404, detail="assignee not found")
        task.assignee_actor_id = body.assignee_actor_id
    if body.reviewer_actor_id is not None:
        if tenant.get(Actor, body.reviewer_actor_id) is None:
            raise HTTPException(status_code=404, detail="reviewer not found")
        task.reviewer_actor_id = body.reviewer_actor_id
    db.commit()
    return {"task_id": task_id, "assignee": task.assignee_actor_id, "reviewer": task.reviewer_actor_id}


@router.post("/artifacts/{artifact_id}/annotate")
def annotate(artifact_id: str, body: AnnotateRequest, db: Session = Depends(get_db),
             p: Principal = Depends(current_principal)) -> dict:
    """Human review of an agent's artifact. With `proposed_rule`, drafts a Playbook amendment
    (a new version) — the correction becomes procedure, not a prompt tweak."""
    tenant = Tenant(db, p.org_id)
    art = tenant.get(Artifact, artifact_id)
    if art is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    reviewer = db.scalars(select(Actor).where(Actor.org_id == p.org_id, Actor.user_id == p.user_id)).first()
    ann = Annotation(org_id=p.org_id, artifact_id=artifact_id,
                     author_actor_id=reviewer.id if reviewer else None, text=body.text,
                     proposed_rule=body.proposed_rule)
    db.add(ann)
    art.reviewed_by_actor_id = reviewer.id if reviewer else art.reviewed_by_actor_id

    amendment = None
    if body.proposed_rule:
        task = db.get(Task, art.task_id)
        if task and task.department_id:
            amendment = playbooks.amend(db, p.org_id, task.department_id, body.proposed_rule,
                                        change_summary=f"From review: {body.text[:80]}")
            ann.amendment_playbook_id = amendment.id
    db.commit()
    return {"annotation_id": ann.id, "amendment_playbook_id": amendment.id if amendment else None}


@router.post("/playbooks/{playbook_id}/activate", response_model=PlaybookOut)
def activate(playbook_id: str, db: Session = Depends(get_db),
             p: Principal = Depends(require_role("ceo", "dept_head"))) -> PlaybookOut:
    pb = Tenant(db, p.org_id).get(Playbook, playbook_id)
    if pb is None:
        raise HTTPException(status_code=404, detail="playbook not found")
    playbooks.activate(db, pb)
    db.commit()
    return _pb_out(pb)


@router.get("/playbooks", response_model=list[PlaybookOut])
def list_playbooks(department_id: str, db: Session = Depends(get_db),
                   p: Principal = Depends(current_principal)) -> list[PlaybookOut]:
    pbs = db.scalars(
        select(Playbook).where(Playbook.org_id == p.org_id, Playbook.department_id == department_id)
        .order_by(Playbook.version.desc())
    )
    return [_pb_out(pb) for pb in pbs]


@router.post("/tasks/{task_id}/rerun")
def rerun(task_id: str, db: Session = Depends(get_db),
          p: Principal = Depends(current_principal)) -> dict:
    from app.services import planning
    task = Tenant(db, p.org_id).get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    project = db.get(Project, task.project_id)
    art = planning.rerun_task(db, project, task)
    return {"artifact_id": art.id, "content": art.content, "version": art.version}


def _pb_out(pb: Playbook) -> PlaybookOut:
    return PlaybookOut(id=pb.id, department_id=pb.department_id, version=pb.version, status=pb.status,
                       change_summary=pb.change_summary, markdown=pb.markdown)
