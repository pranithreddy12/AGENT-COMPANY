"""LeadForge -> Company OS handoff. When LeadForge gets a warm reply / proposal request, it hands
the prospect to Company OS, which owns delivery from here (scope -> proposal -> Legal -> client portal).

Key value: LeadForge's researched pain signals (its crown jewel) flow through as cited evidence on
the Lead AND into the proposal goal, so the drafted proposal is grounded in the real "why", not
generic. Nothing is sent — the resulting proposal still passes Company OS's Critic + Legal + approval,
which matches LeadForge's own human-in-the-loop philosophy.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, Contact, Lead, Project
from app.services import planning


def ingest_handoff(db: Session, org_id: str, hf) -> tuple[Account, Lead, Project, list]:
    # find-or-create the prospect account (a client account, so it's portal-ready)
    account = db.scalars(select(Account).where(Account.org_id == org_id, Account.name == hf.company)).first()
    if account is None:
        account = Account(org_id=org_id, name=hf.company, industry=hf.industry, is_client=True)
        db.add(account)
        db.flush()

    if hf.contact_name:
        db.add(Contact(org_id=org_id, account_id=account.id, name=hf.contact_name, email=hf.contact_email))

    # record the lead as already-qualified (LeadForge qualified it); signals become cited evidence
    evidence = [{"claim": "pain signal", "evidence": s.signal, "source": s.source or "leadforge"}
                for s in hf.signals]
    lead = Lead(
        org_id=org_id, source="leadforge", account_id=account.id, company=hf.company,
        industry=hf.industry, attributes={"context": hf.context, "location": hf.location,
                                          "leadforge_lead_id": hf.leadforge_lead_id},
        qualification_state="qualified", icp_fit_score=100, confidence=1.0, evidence=evidence,
    )
    db.add(lead)
    db.flush()

    # the Lead decomposes a delivery project — signals threaded into the goal so drafts reference them
    signals_line = ("; ".join(s.signal for s in hf.signals)) or "no signals provided"
    goal = hf.goal or (f"Deliver a proposal for {hf.company}"
                       f"{f' ({hf.industry})' if hf.industry else ''}. "
                       f"Address these researched pain signals: {signals_line}."
                       f"{f' They asked: {hf.context}' if hf.context else ''}")
    project, tasks = planning.draft_project(db, org_id, goal, account_id=account.id)
    return account, lead, project, tasks
