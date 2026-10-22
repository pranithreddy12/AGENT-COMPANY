"""The five numbers the roadmap is judged by (plan.md 1b). Pure reads over existing tables - no new
tracking - so they are true by construction and cannot drift from what actually happened."""
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Annotation, Artifact, Project, ProposalAcceptance, Task
from app.services import governance


def _naive(dt):
    """SQLite hands back naive datetimes while objects created this session are tz-aware; subtracting
    them raises. Compare in naive UTC."""
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo is not None else dt


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None  # None = "no data yet", never a misleading 0%


def compute(db: Session, org_id: str) -> dict:
    arts = list(db.scalars(select(Artifact).where(Artifact.org_id == org_id)))
    work = [a for a in arts if a.type != "proposal" and (a.content or "").strip()]
    proposals = [a for a in arts if a.type == "proposal"]

    signed = db.scalar(select(func.count(ProposalAcceptance.id)).where(ProposalAcceptance.org_id == org_id)) or 0
    approved = sum(1 for a in proposals if a.status == "approved")

    first_pass = sum(1 for a in work if a.version == 1 and a.status in ("reviewed", "approved"))
    edited_ids = set(db.scalars(select(Annotation.artifact_id).where(Annotation.org_id == org_id)))
    edited = sum(1 for a in arts if a.id in edited_ids or (a.type == "proposal" and a.version > 1))

    spent = round(governance.spent(db, org_id), 4)

    # time from a project's creation to its first non-proposal artifact (what a user waits for)
    first_out: dict[str, object] = {}
    for a in db.execute(
        select(Task.project_id, Artifact.created_at)
        .join(Artifact, Artifact.task_id == Task.id)
        .where(Artifact.org_id == org_id, Artifact.type != "proposal")
    ):
        ts = _naive(a.created_at)
        if a.project_id not in first_out or ts < first_out[a.project_id]:
            first_out[a.project_id] = ts
    waits = []
    for p in db.scalars(select(Project).where(Project.org_id == org_id, Project.id.in_(list(first_out) or [""]))):
        d = (first_out[p.id] - _naive(p.created_at)).total_seconds()
        if d >= 0:
            waits.append(d)

    return {
        "deals_signed": signed,
        "proposals_generated": len(proposals),
        "proposals_approved": approved,
        "first_pass_rate": _rate(first_pass, len(work)),
        "human_edit_rate": _rate(edited, len(arts)),
        "needs_human_rate": _rate(sum(1 for a in arts if a.needs_human), len(arts)),
        "spent_usd": spent,
        "cost_per_deal_usd": round(spent / signed, 4) if signed else None,
        "cost_per_artifact_usd": round(spent / len(work), 4) if work else None,
        "median_seconds_to_first_output": round(median(waits)) if waits else None,
        "artifacts_total": len(arts),
    }
