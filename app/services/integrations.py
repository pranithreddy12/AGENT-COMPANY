"""LeadForge -> Company OS handoff. When LeadForge gets a warm reply / proposal request, it hands
the prospect to Company OS, which owns delivery from here (scope -> proposal -> Legal -> client portal).

Key value: LeadForge's researched pain signals (its crown jewel) flow through as cited evidence on
the Lead AND into the proposal goal, so the drafted proposal is grounded in the real "why", not
generic. Nothing is sent — the resulting proposal still passes Company OS's Critic + Legal + approval,
which matches LeadForge's own human-in-the-loop philosophy.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Account, Actor, AgentProfile, Artifact, Contact, Department, Lead, Project, Task
from app.services import llm, planning, research, review

_PROPOSAL_SYSTEM = (
    "You are a senior consultant at an automation agency, writing a proposal that will be sent to the "
    "client AS-IS. Write a complete, specific, professional, ready-to-send proposal. Address the "
    "client's actual situation and EACH pain signal with a concrete solution and its business impact. "
    "Use the real client name — NEVER placeholders like [Company Name]. Sections: Executive Summary; "
    "Understanding Your Situation (reference their specific signals); Proposed Solution (one concrete "
    "solution per signal, with the outcome); Scope & Deliverables; Timeline; Investment (only a clearly "
    "marked [price to confirm] for a specific number — nothing else placeholdered); Next Steps. "
    "Professional, concise, no filler, no meta-commentary."
)


def _account_and_lead(db: Session, org_id: str, hf):
    account = db.scalars(select(Account).where(Account.org_id == org_id, Account.name == hf.company)).first()
    if account is None:
        account = Account(org_id=org_id, name=hf.company, industry=hf.industry, is_client=True)
        db.add(account)
        db.flush()
    if hf.contact_name:
        db.add(Contact(org_id=org_id, account_id=account.id, name=hf.contact_name, email=hf.contact_email))
    evidence = [{"claim": "pain signal", "evidence": s.signal, "source": s.source or "leadforge"} for s in hf.signals]
    lead = Lead(org_id=org_id, source="leadforge", account_id=account.id, company=hf.company, industry=hf.industry,
                attributes={"context": hf.context, "location": hf.location, "leadforge_lead_id": hf.leadforge_lead_id},
                qualification_state="qualified", icp_fit_score=100, confidence=1.0, evidence=evidence)
    db.add(lead)
    db.flush()
    return account, lead


def generate_proposal(db: Session, org_id: str, hf) -> dict:
    """One client-ready proposal for a real prospect (the LeadForge 'proposal_requested' path).
    Grounded in the prospect's pain signals + web research, Legal-checked, queued for human approval
    — NOT the generic multi-task decomposition. Returns the proposal text for review/send."""
    account, lead = _account_and_lead(db, org_id, hf)

    research_ctx = ""
    if settings.serper_api_key:  # research the actual prospect for a grounded proposal
        try:
            hits = research.serper_search(f"{hf.company} {hf.industry or ''} {hf.location or ''}".strip(), num=5)
            research_ctx = "\n".join(f"- {h['title']}: {h['snippet']}" for h in hits)
        except Exception:
            pass

    dept = db.scalars(select(Department).where(Department.org_id == org_id, Department.name == "Sales")).first()
    sales = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.department_id == dept.id,
                                           Actor.type == "agent", Actor.role == "member")).first() if dept else None
    prof = db.get(AgentProfile, sales.agent_profile_id) if sales else None
    provider = llm.build_provider(prof.provider, prof.model, settings.anthropic_api_key) if prof else None
    if provider is None:
        return {"error": "no Sales agent configured"}

    signals = "; ".join(s.signal for s in hf.signals) or "none provided"
    user = (f"Client: {hf.company} ({hf.industry or 'business'}{', ' + hf.location if hf.location else ''}).\n"
            f"Their pain signals (address each): {signals}\n"
            f"{'Context: ' + hf.context if hf.context else ''}\n"
            + (f"\nWeb research on the prospect:\n{research_ctx}\n" if research_ctx else "")
            + "\nWrite the full client-ready proposal now.")
    try:
        comp = provider.complete(system=_PROPOSAL_SYSTEM, messages=[{"role": "user", "content": user}],
                                 tools=[], max_tokens=3000)
    except Exception as e:
        return {"error": f"proposal generation failed: {e}"}
    text = (comp.text or "").strip()
    if not text:
        return {"error": "empty proposal"}

    legal = review.legal_review(text)  # no prohibited claims before it can be sent
    project = Project(org_id=org_id, goal=f"Proposal for {hf.company}", account_id=account.id,
                      status="active", health="on_track")
    db.add(project)
    db.flush()
    task = Task(org_id=org_id, project_id=project.id, goal=f"Client proposal for {hf.company}",
                department_id=dept.id if dept else None, assignee_actor_id=sales.id if sales else None,
                status="blocked" if not legal.passed else "done", est_effort_hours=1.0)
    db.add(task)
    db.flush()
    art = Artifact(org_id=org_id, task_id=task.id, type="proposal", content=text,
                   produced_by_actor_id=sales.id if sales else None, status="needs_human", needs_human=True,
                   blocked=not legal.passed, block_reason=None if legal.passed else "; ".join(legal.reasons))
    db.add(art)
    db.flush()
    return {"account_id": account.id, "project_id": project.id, "artifact_id": art.id,
            "proposal": text, "blocked": not legal.passed, "block_reason": art.block_reason,
            "researched": bool(research_ctx)}


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
