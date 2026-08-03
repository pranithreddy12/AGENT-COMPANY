"""Run the Dubai med-spa prospect's delivery project end to end on a local Ollama model.

    python -m scripts.run_dubai          # OLLAMA_MODEL=qwen2.5:7b

Simulates the full loop: LeadForge handoff -> Lead decomposes -> approve (schedule) ->
execute every task through its department agent (real model) -> Critic reviews each ->
Legal veto -> final deliverables. Prints the actual proposal content.
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.config import settings
from app.db import Base
from app.models import Artifact, Department, HandoffPacket, Task
from app.routers.orgs import create_org
from app.schemas import LeadForgeHandoff, LeadForgeSignal, OrgCreate
from app.services import evals, integrations, planning


def p(*a):
    print(*a, flush=True)


def main() -> int:
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
    try:
        have = [m["name"] for m in httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5).json().get("models", [])]
    except Exception as e:
        p(f"Ollama not reachable ({e}).")
        return 1
    if model not in have:
        p(f"{model!r} not installed. Have {have}.")
        return 1

    engine = create_engine("sqlite:///dubai.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    org_id = create_org(OrgCreate(name="Your Agency", ceo_email="c@a.com", ceo_password="pw"), db).org_id
    evals.use_ollama(db, org_id, model=model)
    depts = {d.id: d.name for d in db.scalars(select(Department).where(Department.org_id == org_id))}

    hf = LeadForgeHandoff(
        company="Glow Med-Spa Dubai", industry="med-spa", location="Dubai",
        contact_name="A. Owner", contact_email="owner@glow.ae",
        signals=[LeadForgeSignal(signal="no online booking system", source="google places"),
                 LeadForgeSignal(signal="missed-call complaints in recent reviews", source="review scrape"),
                 LeadForgeSignal(signal="thin opening hours listed", source="google places")],
        context="Can you send me a proposal?")

    p(f"=== Dubai prospect: full delivery run on local {model} ===\n")
    p("[1/3] LeadForge handoff -> Lead decomposing ...")
    account, lead, project, tasks = integrations.ingest_handoff(db, org_id, hf)
    db.commit()
    p(f"  account={account.name} (client) | lead source={lead.source} | signals kept={len(lead.evidence)}")
    p(f"  plan: {len(tasks)} tasks across {len({t.department_id for t in tasks})} departments\n")

    p("[2/3] Approve (schedule + critical path) ...")
    summary = planning.approve_project(db, project)
    p(f"  project due in {summary['project_finish_h']}h | critical path = {len(summary['critical_path'])} tasks\n")

    p("[3/3] Execute end to end (each task -> agent -> Critic -> Legal). This is the slow part ...")
    planning.execute_project(db, project)
    db.commit()

    p(f"\n=== RESULT: project '{project.goal[:50]}...' -> {project.status} ({project.health}) ===")
    packets = db.scalar(select(func.count(HandoffPacket.id)).where(
        HandoffPacket.project_id == project.id)) or 0
    p(f"cross-department handoff packets: {packets}\n")

    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.project_id == project.id))}
    arts = db.scalars(select(Artifact).where(Artifact.task_id.in_(list(tasks)))).all()
    for a in arts:
        t = tasks[a.task_id]
        flags = []
        if a.blocked:
            flags.append(f"BLOCKED({a.block_reason})")
        if a.needs_human:
            flags.append("NEEDS_HUMAN")
        tag = (" [" + ", ".join(flags) + "]") if flags else " [reviewed]"
        p(f"--- {depts.get(t.department_id,'?')}: {t.goal[:60]}{tag}")
        p("    " + a.content.strip().replace("\n", "\n    ")[:700] + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
