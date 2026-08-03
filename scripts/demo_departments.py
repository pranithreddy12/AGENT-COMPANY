"""Exercise Sales + Marketing end to end on a local Ollama model.

    python -m scripts.demo_departments        # OLLAMA_MODEL=qwen2.5:7b default

Shows:
  SALES    - lead qualification (deterministic) -> qualified handoff / disqualified reason
           - the Sales agent drafting a real cold-outreach email (local model)
  MARKETING- the Marketing agent drafting a real launch announcement (local model)
           - governance: a marketing send with a forbidden claim is DENIED; a clean one is QUEUED
"""
import os
import sys

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.config import settings
from app.db import Base
from app.models import Actor, Department, Lead, Organization, Project, Task
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import crm, evals, governance, planning


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # model output may contain emoji/CJK
except Exception:
    pass


def p(*a):
    print(*a, flush=True)


def dept_agent(db, org_id, name):
    dept = db.scalars(select(Department).where(Department.org_id == org_id, Department.name == name)).first()
    agent = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.department_id == dept.id,
                                           Actor.type == "agent", Actor.role == "member")).first()
    return dept, agent


def run_dept_task(db, org_id, name, goal, acceptance):
    """Run one concrete task through a department's agent (loads that dept's Playbook + Critic)."""
    dept, agent = dept_agent(db, org_id, name)
    proj = Project(org_id=org_id, goal=f"{name} demo", status="active", health="on_track")
    db.add(proj)
    db.flush()
    task = Task(org_id=org_id, project_id=proj.id, goal=goal, acceptance_criteria=acceptance,
                department_id=dept.id, assignee_actor_id=agent.id, status="scheduled", est_effort_hours=1.0)
    db.add(task)
    db.flush()
    art = planning.rerun_task(db, proj, task)
    return art


def main() -> int:
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
    try:
        have = [m["name"] for m in httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5).json().get("models", [])]
    except Exception as e:
        p(f"Ollama not reachable ({e}). Run `ollama serve`.")
        return 1
    if model not in have:
        p(f"{model!r} not installed. Have {have}. `ollama pull {model}`.")
        return 1

    engine = create_engine("sqlite:///demo_depts.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    org_id = create_org(OrgCreate(name="DemoCo", ceo_email="c@a.com", ceo_password="pw"), db).org_id
    evals.use_ollama(db, org_id, model=model)
    p(f"=== Departments demo on local {model} ===\n")

    # ---------- SALES: lead qualification (deterministic pipeline) ----------
    p("## SALES — lead qualification")
    strong = Lead(org_id=org_id, source="inbound", company="Globex", industry="fintech", size_employees=250,
                  attributes={"budget": "$120k", "authority": "VP Eng", "need": "replace manual onboarding",
                              "timeline": "this quarter"})
    weak = Lead(org_id=org_id, source="inbound", company="Tiny Farm", industry="agriculture", size_employees=4,
                attributes={})
    db.add_all([strong, weak])
    db.flush()
    crm.qualify(db, strong)
    crm.qualify(db, weak)
    db.commit()
    p(f"  Globex   -> {strong.qualification_state} (ICP {strong.icp_fit_score}, confidence {strong.confidence}), "
      f"handoff={'yes' if strong.handoff_packet_id else 'no'}")
    p(f"  Tiny Farm-> {weak.qualification_state}: {weak.disqualify_reason}\n")

    # ---------- SALES: agent drafts a real cold-outreach email ----------
    p("## SALES — agent drafts cold outreach (local model)")
    art = run_dept_task(db, org_id, "Sales",
                        "Write a cold outreach email to the VP of Engineering at a mid-size fintech, "
                        "introducing our AI onboarding-automation service. Under 120 words, one clear CTA.",
                        "Concise, personalized, one call to action, no unverifiable claims")
    p(f"  [critic: {art.status}, needs_human={art.needs_human}]")
    p("  " + art.content.strip().replace("\n", "\n  ")[:900] + "\n")

    # ---------- MARKETING: agent drafts a launch announcement ----------
    p("## MARKETING — agent drafts launch announcement (local model)")
    art = run_dept_task(db, org_id, "Marketing",
                        "Write a 3-paragraph LinkedIn post announcing the launch of our new client "
                        "referral program. Upbeat but professional, end with a call to action.",
                        "On-brand, no unverified claims, clear CTA")
    p(f"  [critic: {art.status}, needs_human={art.needs_human}]")
    p("  " + art.content.strip().replace("\n", "\n  ")[:900] + "\n")

    # ---------- MARKETING: governance on outbound sends (deterministic) ----------
    p("## MARKETING — governance on outbound")
    org = db.get(Organization, org_id)
    _, mkt = dept_agent(db, org_id, "Marketing")
    try:
        governance.request_outbound(db, org, mkt, "client_send",
                                    "Our product guarantees 300% returns with no risk")
        p("  forbidden-claim send: NOT blocked (unexpected)")
    except governance.Denied as e:
        p(f"  forbidden-claim send: DENIED by rule '{e.rule}'")
    clean = governance.request_outbound(db, org, mkt, "client_send",
                                        "Sharing our Q3 product update and a case study")
    p(f"  clean send: {clean['status']} (rule: {clean.get('rule')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
