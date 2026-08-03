"""Voice router: a call transcript in, a CRM update + correctly-owned follow-up tasks out."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal
from app.db import get_db
from app.models import Call, Lead, Task
from app.schemas import CallCreate, CallOut, FollowUpTaskOut
from app.services import voice
from app.tenancy import Tenant

router = APIRouter(tags=["voice"])


def _call_out(db: Session, call: Call) -> CallOut:
    lead = db.get(Lead, call.lead_id) if call.lead_id else None
    tasks = []
    if call.follow_up_project_id:
        tasks = list(db.scalars(select(Task).where(Task.project_id == call.follow_up_project_id)))
    return CallOut(
        id=call.id, direction=call.direction, consent=call.consent, recording_ref=call.recording_ref,
        summary=call.summary, outcome=call.outcome, extracted_fields=call.extracted_fields,
        lead_id=call.lead_id, lead_state=lead.qualification_state if lead else None,
        follow_up_project_id=call.follow_up_project_id,
        follow_up_tasks=[FollowUpTaskOut(id=t.id, goal=t.goal, department_id=t.department_id,
                                         assignee_actor_id=t.assignee_actor_id, status=t.status) for t in tasks],
    )


@router.post("/calls", response_model=CallOut)
def create_call(body: CallCreate, db: Session = Depends(get_db),
                p: Principal = Depends(current_principal)) -> CallOut:
    call = voice.process_call(
        db, p.org_id, direction=body.direction, from_number=body.from_number, company=body.company,
        transcript=body.transcript, consent=body.consent, recording_ref=body.recording_ref,
        industry=body.industry, size_employees=body.size_employees,
    )
    db.commit()
    return _call_out(db, call)


@router.get("/calls/{call_id}", response_model=CallOut)
def get_call(call_id: str, db: Session = Depends(get_db),
             p: Principal = Depends(current_principal)) -> CallOut:
    call = Tenant(db, p.org_id).get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    return _call_out(db, call)
