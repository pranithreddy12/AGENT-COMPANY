"""Phase 5 gate: lead -> qualified handoff with citations OR disqualified with reason; client
logs in and sees only their project; scope-change routes to Sales."""
from app.auth import Principal
from app.models import HandoffPacket, Lead
from app.routers import crm as crm_router
from app.routers import portal as portal_router
from app.routers.orgs import create_org
from app.schemas import ClientCreate, LeadCreate, PortalMessage, ProjectCreate
from app.services import planning


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


from app.schemas import OrgCreate  # noqa: E402


def _ceo(org_id):
    return Principal("ceo", org_id, "ceo")


def _make_lead(db, org_id, **kw):
    p = _ceo(org_id)
    return crm_router.create_lead(LeadCreate(**kw), db=db, p=p)


def test_strong_lead_qualifies_with_citations(db):
    org_id = _org(db)
    lead = _make_lead(db, org_id, company="Globex", industry="software", size_employees=200,
                      attributes={"budget": "$80k", "authority": "VP Eng", "need": "replace legacy",
                                  "timeline": "this quarter"})
    out = crm_router.qualify_lead(lead.id, db=db, p=_ceo(org_id))
    assert out.qualification_state == "qualified"
    assert out.handoff_packet_id  # structured handoff to Sales
    assert out.confidence == 1.0
    # every claim carries a citation to its source field
    assert all(c["source"].startswith("lead.attributes.") for c in out.evidence)
    packet = db.get(HandoffPacket, out.handoff_packet_id)
    assert packet.evidence and all("[lead.attributes." in e for e in packet.evidence)


def test_poor_icp_is_disqualified_with_reason(db):
    org_id = _org(db)
    lead = _make_lead(db, org_id, company="Farmz", industry="agriculture", size_employees=3, attributes={})
    out = crm_router.qualify_lead(lead.id, db=db, p=_ceo(org_id))
    assert out.qualification_state == "disqualified"
    assert "ICP fit below threshold" in out.disqualify_reason


def test_sparse_lead_says_insufficient_info(db):
    org_id = _org(db)
    lead = _make_lead(db, org_id, company="Initech", industry="software", size_employees=200, attributes={})
    out = crm_router.qualify_lead(lead.id, db=db, p=_ceo(org_id))
    assert out.qualification_state == "insufficient_info"
    assert "insufficient information" in out.disqualify_reason  # never invents fit


def test_client_logs_in_and_sees_only_their_project(db):
    org_id = _org(db)
    client = crm_router.create_client(
        ClientCreate(account_name="Globex", email="client@globex.com", password="pw"), db=db, p=_ceo(org_id))
    # a project for this client + one for another account
    proj, _ = planning.draft_project(db, org_id, "Client engagement", account_id=client.account_id)
    db.commit()
    planning.approve_project(db, proj)
    planning.execute_project(db, proj)
    other, _ = planning.draft_project(db, org_id, "Internal work", account_id="someone-else")
    db.commit()

    cp = Principal(client.user_id, org_id, "client")
    mine = portal_router.my_projects(db=db, p=cp)
    assert len(mine) == 1 and mine[0].id == proj.id
    assert len(mine[0].deliverables) >= 1  # sees reviewed artifacts


def test_scope_change_request_routes_to_sales(db):
    org_id = _org(db)
    client = crm_router.create_client(
        ClientCreate(account_name="Globex", email="c2@globex.com", password="pw"), db=db, p=_ceo(org_id))
    proj, _ = planning.draft_project(db, org_id, "Client engagement", account_id=client.account_id)
    db.commit()
    cp = Principal(client.user_id, org_id, "client")

    r = portal_router.post_request(proj.id, PortalMessage(text="Looks great! I also want a new feature: mobile app"),
                                   db=db, p=cp)
    assert r["scope_change"] is True and r["change_order_id"]

    r2 = portal_router.post_request(proj.id, PortalMessage(text="Thanks, looks good"), db=db, p=cp)
    assert r2["scope_change"] is False
