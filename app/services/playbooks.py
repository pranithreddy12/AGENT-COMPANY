"""Playbooks are the versioned, human-editable company procedure. Agents load the ACTIVE
Playbook at run time; `RULE:` lines are directives the worker applies. Fixing a Playbook (not a
prompt) is how the CEO changes agent behavior — and every agent inherits the fix.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Playbook


def active(db: Session, org_id: str, department_id: str) -> Playbook | None:
    return db.scalars(
        select(Playbook).where(
            Playbook.org_id == org_id, Playbook.department_id == department_id, Playbook.status == "active"
        ).order_by(Playbook.version.desc())
    ).first()


def rules(markdown: str) -> list[str]:
    """Directives an agent must apply, one per `RULE:` line."""
    return [ln.split("RULE:", 1)[1].strip() for ln in (markdown or "").splitlines() if "RULE:" in ln]


def amend(db: Session, org_id: str, department_id: str, new_rule: str, change_summary: str) -> Playbook:
    """Draft the next version (current active + the new RULE). Not active until activated."""
    cur = active(db, org_id, department_id)
    base_md = cur.markdown if cur else f"# Playbook\n"
    next_version = (cur.version if cur else 0) + 1
    draft = Playbook(
        org_id=org_id, department_id=department_id,
        title=cur.title if cur else "Playbook", version=next_version, status="draft",
        markdown=base_md.rstrip() + f"\n\nRULE: {new_rule}", change_summary=change_summary,
    )
    db.add(draft)
    db.flush()
    return draft


def activate(db: Session, playbook: Playbook) -> Playbook:
    """Make a draft the active version; supersede the prior active one."""
    prior = active(db, playbook.org_id, playbook.department_id)
    if prior and prior.id != playbook.id:
        prior.status = "superseded"
    playbook.status = "active"
    db.flush()
    return playbook
