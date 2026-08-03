"""Client portal: a client (role=client) sees only their account's projects, deliverables, and a
single message thread. Requests beyond the SOW are detected and routed to Sales as change orders."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal, require_role
from app.db import get_db
from app.models import Artifact, Message, Project, Task, Thread, User
from app.schemas import ArtifactOut, MessageOut, PortalMessage, PortalProjectOut
from app.services import communication, crm

router = APIRouter(tags=["portal"])


def _account_id(db: Session, p: Principal) -> str:
    user = db.get(User, p.user_id)
    if user is None or user.account_id is None:
        raise HTTPException(status_code=403, detail="not a client account")
    return user.account_id


def _client_project(db: Session, p: Principal, project_id: str) -> Project:
    acct = _account_id(db, p)
    project = db.get(Project, project_id)
    if project is None or project.org_id != p.org_id or project.account_id != acct:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _client_thread(db: Session, project: Project) -> Thread:
    t = db.scalars(select(Thread).where(Thread.project_id == project.id, Thread.thread_type == "client")).first()
    if t is None:
        t = communication.create_thread(db, project.org_id, "client", subject=f"Client thread: {project.goal[:40]}",
                                         project_id=project.id, message_budget=1000)
    return t


def _deliverables(db: Session, project: Project) -> list[Artifact]:
    task_ids = [t.id for t in db.scalars(select(Task).where(Task.project_id == project.id))]
    if not task_ids:
        return []
    # client sees reviewed/approved, non-blocked artifacts
    return list(db.scalars(select(Artifact).where(
        Artifact.task_id.in_(task_ids), Artifact.blocked == False,  # noqa: E712
        Artifact.status.in_(["reviewed", "approved"]))))


@router.get("/portal/projects", response_model=list[PortalProjectOut])
def my_projects(db: Session = Depends(get_db), p: Principal = Depends(require_role("client"))) -> list[PortalProjectOut]:
    acct = _account_id(db, p)
    projects = db.scalars(select(Project).where(Project.org_id == p.org_id, Project.account_id == acct))
    return [_portal_out(db, pr) for pr in projects]


@router.get("/portal/projects/{project_id}", response_model=PortalProjectOut)
def my_project(project_id: str, db: Session = Depends(get_db),
               p: Principal = Depends(require_role("client"))) -> PortalProjectOut:
    return _portal_out(db, _client_project(db, p, project_id))


@router.post("/portal/projects/{project_id}/messages", response_model=MessageOut)
def post_message(project_id: str, body: PortalMessage, db: Session = Depends(get_db),
                 p: Principal = Depends(require_role("client"))) -> MessageOut:
    project = _client_project(db, p, project_id)
    thread = _client_thread(db, project)
    m = communication.post_message(db, thread, None, body.text)
    db.commit()
    return MessageOut(sender_actor_id=None, content=m.content)


@router.post("/portal/projects/{project_id}/requests")
def post_request(project_id: str, body: PortalMessage, db: Session = Depends(get_db),
                 p: Principal = Depends(require_role("client"))) -> dict:
    """A client request. If it exceeds the SOW it becomes a change order routed to Sales."""
    project = _client_project(db, p, project_id)
    if crm.detect_scope_change(body.text):
        co = crm.raise_change_order(db, project, body.text)
        db.commit()
        return {"scope_change": True, "change_order_id": co.id,
                "message": "This looks beyond the current scope — routed to your account team as a change order."}
    thread = _client_thread(db, project)
    communication.post_message(db, thread, None, body.text)
    db.commit()
    return {"scope_change": False, "message": "Request received."}


def _portal_out(db: Session, project: Project) -> PortalProjectOut:
    thread = db.scalars(select(Thread).where(Thread.project_id == project.id, Thread.thread_type == "client")).first()
    msgs = []
    if thread:
        msgs = [MessageOut(sender_actor_id=m.sender_actor_id, content=m.content)
                for m in db.scalars(select(Message).where(Message.thread_id == thread.id).order_by(Message.created_at))]
    dels = _deliverables(db, project)
    return PortalProjectOut(
        id=project.id, goal=project.goal, status=project.status, health=project.health,
        deliverables=[ArtifactOut(id=a.id, task_id=a.task_id, type=a.type, content=a.content, version=a.version,
                                  status=a.status, critic_reasons=a.critic_reasons, needs_human=a.needs_human,
                                  blocked=a.blocked, block_reason=a.block_reason) for a in dels],
        messages=msgs,
    )
