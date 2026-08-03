"""Phase 6 gate: a call transcript updates the CRM and creates correctly-owned tasks, no manual step."""
from sqlalchemy import select

from app.models import Department, Lead, Task
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import voice

TRANSCRIPT = (
    "Hi, this is Dana, I'm the VP of Engineering at Globex. "
    "We're looking to replace our legacy billing system, it's really struggling. "
    "We have budget of around 80k for this. We'd want it done by Q3. "
    "Could you send us a proposal? "
    "Also please send a contract once we agree. "
    "Let's schedule a technical scoping demo next week."
)


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def test_call_updates_crm_and_creates_owned_tasks(db):
    org_id = _org(db)
    call = voice.process_call(db, org_id, direction="inbound", from_number="+1555",
                              company="Globex", transcript=TRANSCRIPT, consent=True,
                              recording_ref="s3://rec/1", industry="software", size_employees=200)
    db.commit()

    # CRM updated: a qualified lead exists from the call
    lead = db.get(Lead, call.lead_id)
    assert lead is not None and lead.source == "voice"
    assert lead.qualification_state == "qualified"  # budget+authority+need+timeline all captured
    assert set(lead.attributes) >= {"budget", "authority", "need", "timeline"}

    # follow-up tasks created and routed to the correct departments
    tasks = list(db.scalars(select(Task).where(Task.project_id == call.follow_up_project_id)))
    depts = {d.id: d.name for d in db.scalars(select(Department).where(Department.org_id == org_id))}
    routed = {depts[t.department_id] for t in tasks}
    assert "Sales" in routed          # "send a proposal"
    assert "Legal" in routed          # "send a contract"
    assert "Development" in routed     # "technical scoping demo"
    # every task has an owner agent (no manual assignment step)
    assert all(t.assignee_actor_id for t in tasks)


def test_consent_gate_drops_recording(db):
    org_id = _org(db)
    call = voice.process_call(db, org_id, direction="inbound", from_number="+1555",
                              company="NoConsentCo", transcript="We might need help. Send a proposal.",
                              consent=False, recording_ref="s3://rec/2")
    db.commit()
    assert call.recording_ref is None  # no consent -> recording not stored


def test_extract_routes_commitments():
    ex = voice.extract("Send me a quote. We'll sign the NDA. Book a demo.")
    depts = {c["department"] for c in ex["commitments"]}
    assert depts == {"Sales", "Legal", "Development"}
