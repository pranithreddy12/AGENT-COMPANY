"""Voice: a provider-abstracted seam + the post-call pipeline. The pipeline shares the same CRM
and Task substrate as text agents — the voice agent is not a separate brain.

Real STT/TTS/telephony live behind VoiceProvider; the demo passes a transcript and StubVoiceProvider
stands in. Extraction is deterministic here (keyword-based); an LLM extractor slots in behind the seam.
"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, Actor, Call, Department, Lead, Project, Task
from app.services import crm, events


class VoiceProvider:
    """Seam for real-time STT/TTS. Demo stub returns nothing useful — the pipeline takes transcripts."""

    def transcribe(self, audio: bytes) -> str:  # pragma: no cover - real provider only
        raise NotImplementedError("wire Deepgram/Whisper behind this seam")

    def synthesize(self, text: str) -> bytes:  # pragma: no cover
        raise NotImplementedError("wire ElevenLabs/Cartesia behind this seam")


class StubVoiceProvider(VoiceProvider):
    pass


# commitment cue -> owning department (first match wins; order matters)
_COMMITMENT_ROUTES = [
    (("proposal", "pricing", "quote", "estimate"), "Sales"),
    (("contract", "agreement", "msa", "nda", "sign"), "Legal"),
    (("demo", "technical", "scoping", "integration", "architecture", "poc"), "Development"),
    (("onboard", "kickoff", "status update", "check in", "check-in"), "Client Management"),
    (("schedule", "follow up", "follow-up", "call back", "email you"), "Client Management"),
]

_FIELD_CUES = {
    "budget": ("budget", "$", "spend", "price range"),
    "authority": ("decision maker", "vp", "cto", "ceo", "owner", "head of", "director"),
    "need": ("need", "problem", "looking for", "pain", "replace", "struggling"),
    "timeline": ("quarter", "month", "weeks", "by ", "asap", "next year", "q1", "q2", "q3", "q4"),
}


def _sentences(transcript: str) -> list[str]:
    import re
    return [s.strip() for s in re.split(r"[.?!\n]+", transcript) if s.strip()]


def extract(transcript: str) -> dict:
    low = transcript.lower()
    attributes = {}
    for field, cues in _FIELD_CUES.items():
        hit = next((s for s in _sentences(transcript) if any(c in s.lower() for c in cues)), None)
        if hit:
            attributes[field] = hit
    commitments = []
    for s in _sentences(transcript):
        sl = s.lower()
        for cues, dept in _COMMITMENT_ROUTES:
            if any(c in sl for c in cues):
                commitments.append({"text": s, "department": dept})
                break
    outcome = "qualified" if len(attributes) >= 3 else ("not_interested" if "not interested" in low else "follow_up")
    return {"attributes": attributes, "commitments": commitments, "outcome": outcome}


def _dept_agent(db: Session, org_id: str, dept_name: str) -> Actor | None:
    dept = db.scalars(select(Department).where(Department.org_id == org_id, Department.name == dept_name)).first()
    if not dept:
        return None
    return db.scalars(select(Actor).where(
        Actor.org_id == org_id, Actor.department_id == dept.id, Actor.type == "agent", Actor.role == "member")).first()


def process_call(db: Session, org_id: str, *, direction: str, from_number: str | None, company: str,
                 transcript: str, consent: bool, recording_ref: str | None = None,
                 industry: str | None = None, size_employees: int | None = None) -> Call:
    ex = extract(transcript)

    call = Call(org_id=org_id, direction=direction, from_number=from_number,
                consent=consent, recording_ref=recording_ref if consent else None,  # consent gate
                transcript=transcript, extracted_fields=ex, outcome=ex["outcome"])
    db.add(call)
    db.flush()
    if recording_ref and not consent:
        events.append(db, org_id=org_id, trace_id=f"call:{call.id}", action="call.recording_dropped",
                      after={"reason": "no consent"})

    # CRM update: account + lead from the call, then qualify (same substrate as text agents)
    account = db.scalars(select(Account).where(Account.org_id == org_id, Account.name == company)).first()
    if account is None:
        account = Account(org_id=org_id, name=company, industry=industry, size_employees=size_employees)
        db.add(account)
        db.flush()
    lead = Lead(org_id=org_id, source="voice", account_id=account.id, company=company,
                industry=industry, size_employees=size_employees, attributes=ex["attributes"])
    db.add(lead)
    db.flush()
    crm.qualify(db, lead)
    call.account_id, call.lead_id = account.id, lead.id

    # Follow-up tasks: one per commitment, owned by the routed department. No manual step.
    project = Project(org_id=org_id, goal=f"Follow up: {company} call", account_id=account.id,
                      status="active", health="on_track")
    db.add(project)
    db.flush()
    from app.models import _now  # local import to avoid cycle at module top
    for c in ex["commitments"]:
        agent = _dept_agent(db, org_id, c["department"])
        dept = db.scalars(select(Department).where(Department.org_id == org_id, Department.name == c["department"])).first()
        db.add(Task(org_id=org_id, project_id=project.id, goal=c["text"],
                    acceptance_criteria="Follow-up commitment from call", department_id=dept.id if dept else None,
                    assignee_actor_id=agent.id if agent else None, status="scheduled",
                    est_effort_hours=1.0, due_at=_now() + timedelta(days=2)))
    call.follow_up_project_id = project.id

    call.summary = (f"{direction.title()} call with {company}. Outcome: {ex['outcome']}. "
                    f"Captured {len(ex['attributes'])} qualification fields, "
                    f"{len(ex['commitments'])} follow-up commitment(s). Lead: {lead.qualification_state}.")
    db.flush()
    events.append(db, org_id=org_id, trace_id=f"call:{call.id}", action="call.processed",
                  after={"lead_id": lead.id, "outcome": ex["outcome"], "tasks": len(ex["commitments"])})
    return call
