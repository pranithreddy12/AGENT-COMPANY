"""CEO console: standup digest API + a single-page dashboard served from static/console.html."""
import os

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal
from app.db import get_db
from app.models import ApprovalRequest, Artifact, Organization, Project, Task, Thread
from app.services import governance, metrics, scheduling

router = APIRouter(tags=["console"])

_HTML_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "console.html")


@router.get("/console/standup")
def standup(db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> dict:
    org = db.get(Organization, p.org_id)
    projects = list(db.scalars(select(Project).where(Project.org_id == p.org_id)))
    tasks = list(db.scalars(select(Task).where(Task.org_id == p.org_id)))

    # at-risk = a scheduled/running task whose remaining effort leaves no buffer (naive: slack < effort*0.2)
    at_risk = [t for t in tasks if t.status in ("scheduled", "running") and scheduling.slip_risk(t.slack_h, t.est_effort_hours, buffer=0.2)]

    pending = db.scalar(select(func.count(ApprovalRequest.id)).where(
        ApprovalRequest.org_id == p.org_id, ApprovalRequest.status == "pending")) or 0
    needs_human = db.scalar(select(func.count(Artifact.id)).where(
        Artifact.org_id == p.org_id, Artifact.needs_human == True)) or 0  # noqa: E712
    blocked_arts = db.scalar(select(func.count(Artifact.id)).where(
        Artifact.org_id == p.org_id, Artifact.blocked == True)) or 0  # noqa: E712
    escalated = db.scalar(select(func.count(Thread.id)).where(
        Thread.org_id == p.org_id, Thread.status == "escalated")) or 0

    return {
        "shipped": {
            "projects_done": sum(1 for x in projects if x.status == "done"),
            "tasks_done": sum(1 for t in tasks if t.status == "done"),
            "projects_total": len(projects),
        },
        "at_risk": {
            "projects_slipping": sum(1 for x in projects if x.health == "slipping"),
            "tasks_at_risk": len(at_risk),
            "tasks_blocked": sum(1 for t in tasks if t.status == "blocked"),
        },
        "needs_you": {
            "pending_approvals": pending,
            "artifacts_need_human": needs_human,
            "artifacts_blocked_by_legal": blocked_arts,
            "escalated_threads": escalated,
        },
        "cost": {
            "spent_usd": round(governance.spent(db, p.org_id), 4),
            "cap_usd": org.cost_cap_usd,
            "remaining_usd": round(governance.remaining_budget(db, org), 4),
        },
        "controls": {"killed": org.killed, "simulation": org.simulation},
    }


@router.get("/console/metrics")
def metrics_view(db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> dict:
    """The five numbers the roadmap is judged by. None means "no data yet", not zero."""
    return metrics.compute(db, p.org_id)


@router.get("/console/readiness")
def readiness(db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> dict:
    """What the org still has to set up before agents can do useful, trustworthy work. Each item names
    the concrete consequence of leaving it empty, so the console can explain instead of just nagging."""
    from app.services import llm

    org = db.get(Organization, p.org_id)
    real_model = bool(org.llm_provider) and org.llm_provider != "echo" and (
        org.llm_provider == "ollama" or bool(llm.resolve_api_key(db, p.org_id, org.llm_provider)))
    items = [
        {"key": "model", "done": real_model, "label": "Connect a real model",
         "why": "Agents are on the built-in demo (Echo) until you pick a provider + key (Governance -> Model)."},
        {"key": "profile", "done": bool((org.profile or "").strip()), "label": "Describe your company",
         "why": "Without it agents only know your name and write generic work that QA rejects."},
        {"key": "prices", "done": bool((org.price_list or "").strip()), "label": "Add your price list",
         "why": "Without it proposals say [price to confirm] everywhere - no invented quotes."},
    ]
    return {"ready": all(i["done"] for i in items), "items": items}


@router.get("/console")
def console_page():
    # no-cache so browsers never serve a stale console after an update
    return FileResponse(_HTML_PATH, media_type="text/html",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
