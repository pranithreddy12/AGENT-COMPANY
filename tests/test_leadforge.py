"""LeadForge -> Company OS handoff: a proposal request becomes a decomposed delivery project,
with LeadForge's researched signals carried through as cited evidence and into the proposal goal."""
from sqlalchemy import select

from app.auth import Principal
from app.models import Account, Contact, Lead, Project
from app.routers import integrations as integ_router
from app.routers.orgs import create_org
from app.schemas import LeadForgeHandoff, LeadForgeSignal, OrgCreate
from app.services import integrations


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def _dubai_handoff():
    return LeadForgeHandoff(
        event="proposal_requested", company="Glow Med-Spa Dubai", industry="med-spa", location="Dubai",
        contact_name="A. Owner", contact_email="owner@glow.ae",
        signals=[LeadForgeSignal(signal="no online booking", source="google places"),
                 LeadForgeSignal(signal="missed-call complaints in reviews", source="review scrape")],
        context="Can you send me a proposal?", leadforge_lead_id="lf_123",
    )


def test_handoff_creates_delivery_project_with_signals(db):
    org_id = _org(db)
    account, lead, project, tasks = integrations.ingest_handoff(db, org_id, _dubai_handoff())
    db.commit()

    # a client account + contact
    assert account.is_client and account.name == "Glow Med-Spa Dubai"
    assert db.scalars(select(Contact).where(Contact.account_id == account.id)).first() is not None

    # the lead is recorded as LeadForge-sourced, already qualified, signals kept as cited evidence
    assert lead.source == "leadforge" and lead.qualification_state == "qualified"
    assert [e["evidence"] for e in lead.evidence] == ["no online booking", "missed-call complaints in reviews"]
    assert lead.evidence[0]["source"] == "google places"

    # the Lead decomposed a delivery project, scoped to the client, with signals threaded into the goal
    assert project.account_id == account.id
    assert len(tasks) >= 3
    assert "no online booking" in project.goal and "proposal" in project.goal.lower()


def test_handoff_endpoint_requires_role(db):
    import pytest
    from fastapi import HTTPException
    from app.auth import require_role
    org_id = _org(db)
    # a plain member can't trigger a handoff
    with pytest.raises(HTTPException) as exc:
        require_role("ceo", "dept_head")(Principal("u", org_id, "member"))
    assert exc.value.status_code == 403
    # a dept_head can
    res = integ_router.leadforge_handoff(_dubai_handoff(), db=db, p=Principal("u", org_id, "dept_head"))
    assert res.project_id and len(res.tasks) >= 3


def test_webhook_secret_auth_end_to_end():
    """LeadForge authenticates with a long-lived X-LeadForge-Secret header (no JWT)."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.models  # noqa: F401
    from app.db import Base, get_db
    from app.main import app as application

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)

    def _override():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    application.dependency_overrides[get_db] = _override
    client = TestClient(application)
    try:
        org = client.post("/orgs", json={"name": "Acme", "ceo_email": "c@a.com", "ceo_password": "pw"}).json()
        ceo = {"authorization": f"Bearer {org['access_token']}"}
        payload = _dubai_handoff().model_dump()

        # no auth at all -> 401
        assert client.post("/integrations/leadforge/handoff", json=payload).status_code == 401

        # generate the webhook secret (ceo only)
        secret = client.post("/integrations/leadforge/secret", headers=ceo).json()["secret"]

        # wrong secret -> 401
        r_bad = client.post("/integrations/leadforge/handoff", json=payload,
                            headers={"x-leadforge-secret": "nope"})
        assert r_bad.status_code == 401

        # correct secret -> 200, project created, no JWT needed
        r = client.post("/integrations/leadforge/handoff", json=payload,
                        headers={"x-leadforge-secret": secret})
        assert r.status_code == 200, r.text
        assert r.json()["project_id"] and len(r.json()["tasks"]) >= 3
    finally:
        application.dependency_overrides.clear()


def test_proposal_path_produces_one_reviewed_proposal(db):
    from app.models import Artifact
    org_id = _org(db)
    out = integrations.generate_proposal(db, org_id, _dubai_handoff())
    db.commit()
    assert "proposal" in out and out["proposal"]           # a single proposal document, not a task list
    assert out["blocked"] is False                          # no prohibited claims
    art = db.get(Artifact, out["artifact_id"])
    assert art.type == "proposal" and art.needs_human is True  # queued for human approval before send


def test_second_handoff_reuses_account(db):
    org_id = _org(db)
    integrations.ingest_handoff(db, org_id, _dubai_handoff())
    db.commit()
    integrations.ingest_handoff(db, org_id, _dubai_handoff())
    db.commit()
    # same company -> one account, two projects
    assert len(list(db.scalars(select(Account).where(Account.org_id == org_id)))) == 1
    assert len(list(db.scalars(select(Project).where(Project.org_id == org_id)))) == 2
