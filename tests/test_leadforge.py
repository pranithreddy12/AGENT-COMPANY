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


def test_proposal_is_idempotent_on_leadforge_lead_id(db):
    """A LeadForge webhook retry (same leadforge_lead_id) must return the EXISTING proposal, not
    generate a second one — a timeout-then-retry can't send the client three proposals."""
    from app.models import Artifact
    org_id = _org(db)
    first = integrations.generate_proposal(db, org_id, _dubai_handoff())
    db.commit()
    second = integrations.generate_proposal(db, org_id, _dubai_handoff())
    db.commit()

    # the retry is a dedup hit pointing at the same proposal
    assert first["idempotent"] is False and second["idempotent"] is True
    assert second["project_id"] == first["project_id"]
    assert second["artifact_id"] == first["artifact_id"]

    # exactly one proposal project + one proposal artifact for this lead
    assert len(list(db.scalars(select(Project).where(Project.org_id == org_id)))) == 1
    assert len(list(db.scalars(select(Artifact).where(Artifact.type == "proposal")))) == 1


def test_proposal_without_lead_id_is_not_deduped(db):
    """No leadforge_lead_id -> no dedup key -> each call is a distinct proposal (NULLs are distinct
    in the unique index, so the constraint never collapses unrelated proposals)."""
    from app.models import Artifact
    org_id = _org(db)
    hf = _dubai_handoff()
    hf.leadforge_lead_id = None
    integrations.generate_proposal(db, org_id, hf)
    db.commit()
    integrations.generate_proposal(db, org_id, hf)
    db.commit()
    assert len(list(db.scalars(select(Project).where(Project.org_id == org_id)))) == 2
    assert len(list(db.scalars(select(Artifact).where(Artifact.type == "proposal")))) == 2


def test_async_proposal_shell_then_ready(db):
    """The async path: start_proposal returns a 'generating' shell instantly (no LLM); a second
    request before it finishes dedups to the same shell; the background produce step fills it to
    'ready' with a human-gated artifact."""
    org_id = _org(db)
    project, is_new = integrations.start_proposal(db, org_id, _dubai_handoff())
    db.commit()
    assert is_new and project.status == "generating"

    # a retry while still generating -> same shell, no new project, no new work
    p2, is_new2 = integrations.start_proposal(db, org_id, _dubai_handoff())
    assert not is_new2 and p2.id == project.id

    # background half fills the proposal
    art, _researched = integrations._produce_proposal_artifact(db, project, _dubai_handoff())
    db.commit()
    assert project.status == "ready"
    assert art is not None and art.type == "proposal" and art.needs_human is True


def test_stuck_generating_recovered_and_retryable(db):
    """A restart mid-generation must not leave a proposal stuck: recover_stuck marks it 'failed' and
    frees the dedup slot so a LeadForge retry can regenerate instead of polling forever."""
    from app.main import recover_stuck
    org_id = _org(db)
    project, _ = integrations.start_proposal(db, org_id, _dubai_handoff())
    db.commit()
    assert project.status == "generating" and project.leadforge_lead_id == "lf_123"

    recover_stuck(db)
    db.refresh(project)
    assert project.status == "failed" and project.leadforge_lead_id is None

    # slot freed -> a retry is a fresh generation, not a dedup hit on the dead one
    _p2, is_new = integrations.start_proposal(db, org_id, _dubai_handoff())
    assert is_new


def test_proposal_text_released_only_after_human_approval(db):
    """The send gate: a generated ('ready') proposal returns status but NO text until a human
    approves; after approval the text is released."""
    org_id = _org(db)
    project, _ = integrations.start_proposal(db, org_id, _dubai_handoff())
    integrations._produce_proposal_artifact(db, project, _dubai_handoff())
    db.commit()

    pre = integrations.proposal_view(db, org_id, project.id)
    assert pre["status"] == "ready" and pre["ready"] is False and "proposal" not in pre

    approved = integrations.approve_proposal(db, org_id, project.id)
    db.commit()
    assert approved["status"] == "approved"
    post = integrations.proposal_view(db, org_id, project.id)
    assert post["ready"] is True and post["status"] == "approved" and post["proposal"]


def test_cannot_approve_or_release_legally_blocked_proposal(db):
    """A Legal-blocked proposal can't be approved and its text is never released by the gate."""
    hf = _dubai_handoff()
    hf.signals = [LeadForgeSignal(signal="we promise guaranteed returns to every client", source="x")]
    org_id = _org(db)
    project, _ = integrations.start_proposal(db, org_id, hf)
    integrations._produce_proposal_artifact(db, project, hf)
    db.commit()

    art = integrations._proposal_artifact(db, project)
    assert art.blocked is True  # echoed proposal contains "guaranteed returns" -> Legal veto

    assert integrations.approve_proposal(db, org_id, project.id)["error"] == "blocked"
    view = integrations.proposal_view(db, org_id, project.id)
    assert view["ready"] is False and view["blocked"] is True and "proposal" not in view


def test_proposal_view_is_none_for_non_proposal_project(db):
    """A regular delivery project (from /handoff) is not a proposal — the proposal API 404s on it."""
    org_id = _org(db)
    _a, _l, project, _t = integrations.ingest_handoff(db, org_id, _dubai_handoff())
    db.commit()
    assert integrations.proposal_view(db, org_id, project.id) is None
    assert integrations.approve_proposal(db, org_id, project.id) is None


def test_proposal_get_and_approve_over_http():
    """End-to-end HTTP: POST returns a proposal_id (no text); GET withholds text until a ceo approves,
    then GET releases it. Exercises the real threaded endpoint + the approval gate."""
    import time

    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.models  # noqa: F401
    from app.db import Base, get_db
    from app.main import app as application
    from app.services import integrations as integ_svc

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
    saved_session_local = integ_svc.SessionLocal
    integ_svc.SessionLocal = TestingSession  # background worker uses the same in-memory DB
    client = TestClient(application)
    try:
        org = client.post("/orgs", json={"name": "Acme", "ceo_email": "c@a.com", "ceo_password": "pw"}).json()
        ceo = {"authorization": f"Bearer {org['access_token']}"}
        payload = _dubai_handoff().model_dump()

        started = client.post("/integrations/leadforge/proposal", json=payload, headers=ceo).json()
        pid = started["proposal_id"]
        assert started["status"] == "generating" and "proposal" not in started

        # wait for the background thread to finish generating
        for _ in range(50):
            time.sleep(0.1)
            got = client.get(f"/proposals/{pid}", headers=ceo).json()
            if got["status"] != "generating":
                break
        assert got["status"] == "ready" and got["ready"] is False and "proposal" not in got  # gate holds

        approved = client.post(f"/proposals/{pid}/approve", headers=ceo)
        assert approved.status_code == 200, approved.text

        released = client.get(f"/proposals/{pid}", headers=ceo).json()
        assert released["ready"] is True and released["status"] == "approved" and released["proposal"]
    finally:
        integ_svc.SessionLocal = saved_session_local
        application.dependency_overrides.clear()


def test_generation_failure_marks_failed_and_frees_slot(db):
    """If generation can't run (no Sales agent / provider), the proposal is marked 'failed' and its
    dedup slot is freed, so a LeadForge retry regenerates instead of being stuck on a dead proposal."""
    from app.models import Actor, Department
    org_id = _org(db)
    sales = db.scalars(select(Department).where(Department.org_id == org_id, Department.name == "Sales")).first()
    for a in db.scalars(select(Actor).where(Actor.department_id == sales.id)):
        db.delete(a)  # no Sales member -> no provider can be built
    db.flush()

    project, _ = integrations.start_proposal(db, org_id, _dubai_handoff())
    art, _researched = integrations._produce_proposal_artifact(db, project, _dubai_handoff())
    db.commit()
    assert art is None and project.status == "failed" and project.leadforge_lead_id is None

    # slot freed -> a retry is a fresh attempt, not a dedup hit on the dead proposal
    _p2, is_new = integrations.start_proposal(db, org_id, _dubai_handoff())
    assert is_new


def _approved_proposal(db, org_id):
    """Helper: a generated + approved proposal, returns (project, share_token)."""
    project, _ = integrations.start_proposal(db, org_id, _dubai_handoff())
    integrations._produce_proposal_artifact(db, project, _dubai_handoff())
    db.commit()
    token = integrations.approve_proposal(db, org_id, project.id)["accept_token"]
    db.commit()
    return project, token


def test_approve_mints_share_token_and_public_view_releases_text(db):
    org_id = _org(db)
    project, token = _approved_proposal(db, org_id)
    assert token and project.accept_token == token
    pv = integrations.public_proposal(db, token)
    assert pv is not None
    _proj, art, acc = pv
    assert acc is None and art.content and not art.blocked
    assert integrations.public_proposal(db, "not-a-real-token") is None  # unknown token -> nothing


def test_public_accept_records_signature_and_is_idempotent(db):
    from app.models import ProposalAcceptance
    org_id = _org(db)
    project, token = _approved_proposal(db, org_id)

    r1 = integrations.accept_proposal_by_token(db, token, "Jane Owner", "1.2.3.4")
    db.commit()
    assert r1["accepted"] is True and r1["idempotent"] is False
    db.refresh(project)
    assert project.status == "accepted"
    rows = list(db.scalars(select(ProposalAcceptance).where(ProposalAcceptance.project_id == project.id)))
    assert len(rows) == 1 and rows[0].signer_name == "Jane Owner" and rows[0].content_sha256

    # a second accept (double-click, or a different name) returns the FIRST signature, no new row
    r2 = integrations.accept_proposal_by_token(db, token, "Someone Else", "9.9.9.9")
    db.commit()
    assert r2["idempotent"] is True and r2["signer_name"] == "Jane Owner"
    assert len(list(db.scalars(select(ProposalAcceptance).where(ProposalAcceptance.project_id == project.id)))) == 1
    # an accepted proposal is still viewable (text stays released)
    assert integrations.proposal_view(db, org_id, project.id)["accepted"] is True


def test_accept_rejects_empty_name_and_unknown_token(db):
    org_id = _org(db)
    _project, token = _approved_proposal(db, org_id)
    assert integrations.accept_proposal_by_token(db, token, "   ", "1.1.1.1")["error"] == "empty_name"
    assert integrations.accept_proposal_by_token(db, "bogus-token", "Jane", None) is None


def test_unapproved_proposal_has_no_public_link(db):
    """A generated-but-not-approved proposal never gets a token, so its text can't leak via /p."""
    org_id = _org(db)
    project, _ = integrations.start_proposal(db, org_id, _dubai_handoff())
    integrations._produce_proposal_artifact(db, project, _dubai_handoff())
    db.commit()
    assert project.accept_token is None


def test_public_accept_page_and_form_over_http():
    """End-to-end HTTP through the real client page: GET /p/{token} renders the proposal + accept form,
    a bad token 404s, an empty name 400s, and POSTing a name flips the deal to accepted."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.models  # noqa: F401
    from app.db import Base, get_db
    from app.main import app as application
    from app.models import Project
    from app.services import integrations as isvc

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
        s = TestingSession()
        org_id = create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), s).org_id
        out = isvc.generate_proposal(s, org_id, _dubai_handoff())
        s.commit()
        isvc.approve_proposal(s, org_id, out["project_id"])
        s.commit()
        token = s.get(Project, out["project_id"]).accept_token
        s.close()

        page = client.get(f"/p/{token}")
        assert page.status_code == 200 and "Accept proposal" in page.text  # form present, not yet signed

        assert client.get("/p/nope").status_code == 404  # unknown token

        empty = client.post(f"/p/{token}/accept", data={"signer_name": "  "})
        assert empty.status_code == 400 and "type your name" in empty.text.lower()

        signed = client.post(f"/p/{token}/accept", data={"signer_name": "Jane Owner"})  # follows redirect
        assert signed.status_code == 200 and "Accepted by Jane Owner" in signed.text
        assert "Accept proposal" not in signed.text  # form gone once signed
    finally:
        application.dependency_overrides.clear()


def test_second_handoff_reuses_account(db):
    org_id = _org(db)
    integrations.ingest_handoff(db, org_id, _dubai_handoff())
    db.commit()
    integrations.ingest_handoff(db, org_id, _dubai_handoff())
    db.commit()
    # same company -> one account, two projects
    assert len(list(db.scalars(select(Account).where(Account.org_id == org_id)))) == 1
    assert len(list(db.scalars(select(Project).where(Project.org_id == org_id)))) == 2
