"""CRM: leads + evidence-cited qualification, and client account/user creation."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal, hash_password, issue_token, require_role
from app.db import get_db
from app.models import Account, Lead, User
from app.schemas import ClientCreate, ClientCreated, LeadCreate, LeadOut
from app.services import crm
from app.tenancy import Tenant

router = APIRouter(tags=["crm"])


def _lead_out(lead: Lead) -> LeadOut:
    return LeadOut(
        id=lead.id, company=lead.company, qualification_state=lead.qualification_state,
        icp_fit_score=lead.icp_fit_score, confidence=lead.confidence, framework=lead.framework,
        disqualify_reason=lead.disqualify_reason, evidence=lead.evidence, handoff_packet_id=lead.handoff_packet_id,
    )


@router.get("/leads", response_model=list[LeadOut])
def list_leads(db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> list[LeadOut]:
    from sqlalchemy import select
    return [_lead_out(l) for l in db.scalars(select(Lead).where(Lead.org_id == p.org_id).order_by(Lead.created_at.desc()))]


@router.post("/leads", response_model=LeadOut)
def create_lead(body: LeadCreate, db: Session = Depends(get_db),
                p: Principal = Depends(current_principal)) -> LeadOut:
    lead = Lead(org_id=p.org_id, source=body.source, company=body.company, industry=body.industry,
                size_employees=body.size_employees, attributes=body.attributes)
    db.add(lead)
    db.commit()
    return _lead_out(lead)


@router.post("/leads/{lead_id}/qualify", response_model=LeadOut)
def qualify_lead(lead_id: str, db: Session = Depends(get_db),
                 p: Principal = Depends(current_principal)) -> LeadOut:
    lead = Tenant(db, p.org_id).get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    crm.qualify(db, lead)
    db.commit()
    return _lead_out(lead)


@router.get("/leads/{lead_id}", response_model=LeadOut)
def get_lead(lead_id: str, db: Session = Depends(get_db),
             p: Principal = Depends(current_principal)) -> LeadOut:
    lead = Tenant(db, p.org_id).get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return _lead_out(lead)


@router.post("/clients", response_model=ClientCreated)
def create_client(body: ClientCreate, db: Session = Depends(get_db),
                  p: Principal = Depends(require_role("ceo", "dept_head"))) -> ClientCreated:
    """Create a client Account + a client-role login scoped to it."""
    account = Account(org_id=p.org_id, name=body.account_name, industry=body.industry, is_client=True)
    db.add(account)
    db.flush()
    user = User(org_id=p.org_id, email=body.email, pw_hash=hash_password(body.password),
                role="client", account_id=account.id)
    db.add(user)
    db.commit()
    return ClientCreated(account_id=account.id, user_id=user.id, access_token=issue_token(user))
