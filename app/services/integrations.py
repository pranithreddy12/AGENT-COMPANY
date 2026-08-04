"""LeadForge -> Company OS handoff. When LeadForge gets a warm reply / proposal request, it hands
the prospect to Company OS, which owns delivery from here (scope -> proposal -> Legal -> client portal).

Key value: LeadForge's researched pain signals (its crown jewel) flow through as cited evidence on
the Lead AND into the proposal goal, so the drafted proposal is grounded in the real "why", not
generic. Nothing is sent automatically — a proposal passes a coarse keyword screen and then a human
must approve it before LeadForge can fetch and send the text (see proposal_view / approve_proposal),
matching LeadForge's own human-in-the-loop model.
"""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
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


def _proposal_artifact(db: Session, project: Project) -> Artifact | None:
    """The single proposal Artifact for a proposal Project (via its Task). None while still
    'generating' or if generation failed."""
    task = db.scalars(select(Task).where(Task.project_id == project.id)).first()
    return db.scalars(select(Artifact).where(Artifact.task_id == task.id, Artifact.type == "proposal")
                      ).first() if task else None


def _proposal_result(db: Session, project: Project, *, idempotent: bool = False,
                     researched: bool = False) -> dict:
    """Rebuild the proposal response from a stored proposal Project (its Task -> Artifact). One shape
    whether the proposal was just generated or returned from a dedup hit."""
    art = _proposal_artifact(db, project)
    return {"account_id": project.account_id, "project_id": project.id,
            "artifact_id": art.id if art else None,
            "proposal": art.content if art else "", "blocked": bool(art and art.blocked),
            "block_reason": art.block_reason if art else None,
            "researched": researched, "idempotent": idempotent}


def _proposal_project(db: Session, org_id: str, proposal_id: str) -> Project | None:
    """Fetch a proposal Project scoped to the org, or None if it isn't one. A regular delivery
    project (no proposal artifact and not in a proposal-generation status) is not a proposal."""
    project = db.scalars(select(Project).where(Project.org_id == org_id, Project.id == proposal_id)).first()
    if project is None:
        return None
    if _proposal_artifact(db, project) is None and project.status not in ("generating", "failed"):
        return None
    return project


def proposal_view(db: Session, org_id: str, proposal_id: str) -> dict | None:
    """What LeadForge (or a human) sees when fetching a proposal. Releases the proposal TEXT only
    once a human has approved it AND it isn't Legal-blocked — this is the real send gate. Otherwise
    returns status only. None if there's no such proposal in this org."""
    project = _proposal_project(db, org_id, proposal_id)
    if project is None:
        return None
    art = _proposal_artifact(db, project)
    approved = bool(art and art.status == "approved" and not art.blocked)
    out = {"proposal_id": project.id, "status": project.status, "ready": approved,
           "blocked": bool(art and art.blocked), "block_reason": art.block_reason if art else None}
    if approved:
        out["proposal"] = art.content  # the ONLY place sendable text leaves the system
    return out


def approve_proposal(db: Session, org_id: str, proposal_id: str) -> dict | None:
    """Human approval: clears needs_human and marks the proposal approved so its text can be fetched
    and sent. Refuses a Legal-blocked proposal (override the veto first) and one still generating.
    None if there's no such proposal in this org."""
    project = _proposal_project(db, org_id, proposal_id)
    if project is None:
        return None
    art = _proposal_artifact(db, project)
    if art is None:
        return {"error": "not_ready"}  # still generating / failed — nothing to approve yet
    if art.blocked:
        return {"error": "blocked", "block_reason": art.block_reason}  # override the Legal veto first
    art.needs_human, art.status = False, "approved"
    project.status = "approved"
    return {"proposal_id": project.id, "status": "approved", "ready": True}


def _existing_proposal(db: Session, org_id: str, leadforge_lead_id: str | None) -> Project | None:
    if not leadforge_lead_id:
        return None  # no dedup key -> can't dedup (NULLs are distinct in the unique index)
    return db.scalars(select(Project).where(
        Project.org_id == org_id, Project.leadforge_lead_id == leadforge_lead_id)).first()


def start_proposal(db: Session, org_id: str, hf) -> tuple[Project, bool]:
    """Fast, synchronous: create the proposal shell (account + lead + a 'generating' Project keyed by
    leadforge_lead_id) and return (project, is_new). No LLM here — so the webhook returns instantly.
    Idempotent: an existing proposal for the same lead returns (existing, False) and starts no new
    work. The unique index + IntegrityError catch closes the concurrent-retry race."""
    existing = _existing_proposal(db, org_id, hf.leadforge_lead_id)
    if existing is not None:
        return existing, False

    account, _lead = _account_and_lead(db, org_id, hf)
    project = Project(org_id=org_id, goal=f"Proposal for {hf.company}", account_id=account.id,
                      status="generating", health="on_track", leadforge_lead_id=hf.leadforge_lead_id)
    db.add(project)
    try:
        db.flush()  # unique (org_id, leadforge_lead_id) — a concurrent duplicate raises here
    except IntegrityError:
        db.rollback()  # the other request won the race; drop our account/lead work
        existing = _existing_proposal(db, org_id, hf.leadforge_lead_id)
        if existing is not None:
            return existing, False
        raise  # collision but no row found — genuinely unexpected, don't swallow it
    return project, True


def _fail_proposal(project: Project) -> None:
    """Mark a proposal generation failed and free its dedup slot so a webhook retry can regenerate
    (a stuck/failed proposal must not block the client from ever getting one)."""
    project.status = "failed"
    project.leadforge_lead_id = None


def _produce_proposal_artifact(db: Session, project: Project, hf) -> tuple[Artifact | None, bool]:
    """The slow half (runs in the background thread): research + LLM + Legal -> Task + Artifact under
    an existing proposal Project. Sets project.status to 'ready' (awaiting human approval) or, on any
    failure, 'failed'. Returns (artifact, researched); (None, False) on failure. Uses project.org_id
    so it needs only the project + the handoff data, not the request session."""
    org_id = project.org_id
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
        _fail_proposal(project)
        return None, False

    signals = "; ".join(s.signal for s in hf.signals) or "none provided"
    user = (f"Client: {hf.company} ({hf.industry or 'business'}{', ' + hf.location if hf.location else ''}).\n"
            f"Their pain signals (address each): {signals}\n"
            f"{'Context: ' + hf.context if hf.context else ''}\n"
            + (f"\nWeb research on the prospect:\n{research_ctx}\n" if research_ctx else "")
            + "\nWrite the full client-ready proposal now.")
    try:
        comp = provider.complete(system=_PROPOSAL_SYSTEM, messages=[{"role": "user", "content": user}],
                                 tools=[], max_tokens=3000)
    except Exception:
        _fail_proposal(project)
        return None, False
    text = (comp.text or "").strip()
    if not text:
        _fail_proposal(project)
        return None, False

    legal = review.legal_review(text)  # coarse keyword screen only — human approval is the real send gate
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
    project.status = "ready"  # generated; awaiting human approval before LeadForge may send
    return art, bool(research_ctx)


def run_proposal_in_background(project_id: str, hf) -> None:
    """Background worker for the async /proposal endpoint: own session/thread so the webhook returns
    at once. Mirrors projects._run_in_background. On any error the proposal is marked 'failed' (and
    its dedup slot freed) so LeadForge never polls a stuck 'generating' forever."""
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is None or project.status != "generating":
            return  # already handled (e.g. a concurrent finisher) — don't double-produce
        _produce_proposal_artifact(db, project, hf)
        db.commit()
    except Exception:
        db.rollback()
        project = db.get(Project, project_id)
        if project and project.status == "generating":
            _fail_proposal(project)
            db.commit()
    finally:
        db.close()


def generate_proposal(db: Session, org_id: str, hf) -> dict:
    """Synchronous full generate (shell + content in one call). Used directly (tests, scripts). The
    async webhook path uses start_proposal + run_proposal_in_background instead so it never blocks."""
    project, is_new = start_proposal(db, org_id, hf)
    if not is_new:
        return _proposal_result(db, project, idempotent=True)
    art, researched = _produce_proposal_artifact(db, project, hf)
    if art is None:
        return {"error": "proposal generation failed", "proposal_id": project.id, "status": project.status}
    return _proposal_result(db, project, researched=researched)


def ingest_handoff(db: Session, org_id: str, hf) -> tuple[Account, Lead, Project, list]:
    # find-or-create the client account + record the already-qualified lead (one source of truth,
    # shared with the proposal path)
    account, lead = _account_and_lead(db, org_id, hf)

    # the Lead decomposes a delivery project — signals threaded into the goal so drafts reference them
    signals_line = ("; ".join(s.signal for s in hf.signals)) or "no signals provided"
    goal = hf.goal or (f"Deliver a proposal for {hf.company}"
                       f"{f' ({hf.industry})' if hf.industry else ''}. "
                       f"Address these researched pain signals: {signals_line}."
                       f"{f' They asked: {hf.context}' if hf.context else ''}")
    project, tasks = planning.draft_project(db, org_id, goal, account_id=account.id)
    return account, lead, project, tasks
