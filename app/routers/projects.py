"""Phase 1 gate over HTTP: goal -> reviewable DAG -> approve -> execute -> slip recompute."""
import threading

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal, require_role
from app.db import SessionLocal, get_db
from app.models import Artifact, Department, HandoffPacket, MemoryRecord, Message, Project, Task, Thread
from app.schemas import (
    ArtifactOut, HandoffOut, MessageOut, ProjectCreate, ProjectOut, SlipRequest, ThreadOut, TaskOut,
)
from app.services import planning, scheduling
from app.tenancy import Tenant

router = APIRouter(tags=["projects"])


def _artifact_out(a: Artifact) -> ArtifactOut:
    return ArtifactOut(
        id=a.id, task_id=a.task_id, type=a.type, content=a.content, version=a.version, status=a.status,
        critic_reasons=a.critic_reasons, needs_human=a.needs_human, blocked=a.blocked, block_reason=a.block_reason,
    )


def _task_out(t: Task) -> TaskOut:
    return TaskOut(
        id=t.id, goal=t.goal, acceptance_criteria=t.acceptance_criteria,
        department_id=t.department_id, assignee_actor_id=t.assignee_actor_id,
        depends_on=list(t.depends_on), est_effort_hours=t.est_effort_hours, status=t.status,
        est_start_h=t.est_start_h, est_finish_h=t.est_finish_h, slack_h=t.slack_h,
        is_critical=t.is_critical, due_at=t.due_at,
    )


def _project_out(db: Session, project: Project) -> ProjectOut:
    tasks = list(db.scalars(select(Task).where(Task.project_id == project.id)))
    nodes = [{"id": t.id, "effort": t.est_effort_hours, "deps": list(t.depends_on)} for t in tasks]
    crit = scheduling.schedule(nodes)[2] if nodes else []
    return ProjectOut(
        id=project.id, goal=project.goal, status=project.status, health=project.health,
        start_at=project.start_at, due_at=project.due_at, critical_path=crit,
        tasks=[_task_out(t) for t in tasks],
    )


def _load(db: Session, p: Principal, project_id: str) -> Project:
    project = Tenant(db, p.org_id).get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.post("/projects", response_model=ProjectOut)
def create_project(body: ProjectCreate, db: Session = Depends(get_db),
                   p: Principal = Depends(require_role("ceo", "dept_head"))) -> ProjectOut:
    try:
        project, _ = planning.draft_project(db, p.org_id, body.goal, account_id=body.account_id)
    except planning.PlanError as e:
        raise HTTPException(status_code=422, detail=str(e))
    db.commit()
    return _project_out(db, project)


@router.post("/projects/{project_id}/approve", response_model=ProjectOut)
def approve(project_id: str, db: Session = Depends(get_db),
            p: Principal = Depends(require_role("ceo", "dept_head"))) -> ProjectOut:
    project = _load(db, p, project_id)
    planning.approve_project(db, project)
    return _project_out(db, project)


_exec_lock = threading.Lock()


def _run_in_background(project_id: str) -> None:
    """Execute a project in its own session/thread so the HTTP request returns immediately.
    Agents can take minutes each (local model) — the UI polls status/chat instead of hanging."""
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        planning.execute_project(db, project)
    except Exception:
        db.rollback()
        project = db.get(Project, project_id)
        if project and project.status == "executing":
            project.status = "active"  # revert so it can be retried
            db.commit()
    finally:
        db.close()


@router.post("/projects/{project_id}/execute")
def execute(project_id: str, db: Session = Depends(get_db),
            p: Principal = Depends(current_principal)) -> dict:
    project = _load(db, p, project_id)
    # guard: one execution at a time per project (check-and-set under a lock)
    with _exec_lock:
        db.refresh(project)
        if project.status == "executing":
            raise HTTPException(status_code=409, detail="already executing — watch the Team chat")
        project.status = "executing"
        db.commit()
    threading.Thread(target=_run_in_background, args=(project.id,), daemon=True).start()
    return {"status": "executing", "message": "Agents are working. Watch the Team chat — refresh to see progress."}


@router.get("/projects/{project_id}/memory")
def project_memory(project_id: str, db: Session = Depends(get_db),
                   p: Principal = Depends(current_principal)) -> list[dict]:
    _load(db, p, project_id)
    mem = db.scalars(select(MemoryRecord).where(MemoryRecord.project_id == project_id)
                     .order_by(MemoryRecord.created_at))
    return [{"scope": m.scope, "department_id": m.department_id, "content": m.content} for m in mem]


@router.get("/projects/{project_id}/handoffs", response_model=list[HandoffOut])
def handoffs(project_id: str, db: Session = Depends(get_db),
             p: Principal = Depends(current_principal)) -> list[HandoffOut]:
    _load(db, p, project_id)
    packets = db.scalars(select(HandoffPacket).where(HandoffPacket.project_id == project_id))
    return [
        HandoffOut(id=h.id, from_department_id=h.from_department_id, to_department_id=h.to_department_id,
                   context=h.context, evidence=list(h.evidence), open_questions=list(h.open_questions),
                   confidence=h.confidence)
        for h in packets
    ]


@router.get("/projects/{project_id}/threads", response_model=list[ThreadOut])
def threads(project_id: str, db: Session = Depends(get_db),
            p: Principal = Depends(current_principal)) -> list[ThreadOut]:
    _load(db, p, project_id)
    out = []
    for t in db.scalars(select(Thread).where(Thread.project_id == project_id)):
        msgs = db.scalars(select(Message).where(Message.thread_id == t.id).order_by(Message.created_at))
        out.append(ThreadOut(id=t.id, thread_type=t.thread_type, subject=t.subject, status=t.status,
                             message_budget=t.message_budget,
                             messages=[MessageOut(sender_actor_id=m.sender_actor_id, content=m.content) for m in msgs]))
    return out


@router.post("/artifacts/{artifact_id}/override", response_model=ArtifactOut)
def override_veto(artifact_id: str, db: Session = Depends(get_db),
                  p: Principal = Depends(require_role("ceo", "dept_head"))) -> ArtifactOut:
    """Clear a Legal veto. Human-only (ceo/dept_head) — no agent path can reach this."""
    art = Tenant(db, p.org_id).get(Artifact, artifact_id)
    if art is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    art.blocked, art.block_reason = False, None
    art.status = "approved"
    db.commit()
    return _artifact_out(art)


@router.get("/departments")
def list_departments(db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> list[dict]:
    return [{"id": d.id, "name": d.name, "charter": d.charter, "paused": d.paused}
            for d in db.scalars(select(Department).where(Department.org_id == p.org_id).order_by(Department.name))]


@router.get("/projects")
def list_projects(db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> list[dict]:
    projs = list(db.scalars(select(Project).where(Project.org_id == p.org_id).order_by(Project.created_at.desc())))
    # one grouped count for the whole org instead of a COUNT(*) per project (was N+1)
    counts = dict(db.execute(select(Task.project_id, func.count(Task.id))
                             .where(Task.org_id == p.org_id).group_by(Task.project_id)).all())
    return [{"id": pr.id, "goal": pr.goal, "status": pr.status, "health": pr.health,
             "due_at": pr.due_at.isoformat() if pr.due_at else None, "account_id": pr.account_id,
             "tasks": counts.get(pr.id, 0)} for pr in projs]


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, db: Session = Depends(get_db),
                p: Principal = Depends(current_principal)) -> ProjectOut:
    return _project_out(db, _load(db, p, project_id))


@router.get("/projects/{project_id}/artifacts")
def project_artifacts(project_id: str, db: Session = Depends(get_db),
                      p: Principal = Depends(current_principal)) -> list[dict]:
    _load(db, p, project_id)
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.project_id == project_id))}
    if not tasks:
        return []
    arts = db.scalars(select(Artifact).where(Artifact.task_id.in_(list(tasks))))
    return [{"id": a.id, "task_goal": tasks[a.task_id].goal, "department_id": tasks[a.task_id].department_id,
             "status": a.status, "blocked": a.blocked, "block_reason": a.block_reason,
             "needs_human": a.needs_human, "version": a.version, "content": a.content} for a in arts]


@router.post("/tasks/{task_id}/slip", response_model=ProjectOut)
def slip(task_id: str, body: SlipRequest, db: Session = Depends(get_db),
         p: Principal = Depends(current_principal)) -> ProjectOut:
    task = Tenant(db, p.org_id).get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    project = _load(db, p, task.project_id)
    planning.slip_task(db, project, task, body.added_hours)
    return _project_out(db, project)
