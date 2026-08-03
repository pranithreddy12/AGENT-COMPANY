"""Phase 3 gate + governance guarantees."""
import pytest
from sqlalchemy import select

from app.models import Actor, AgentRun, Department, Organization
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import governance as g


def _org(db):
    org_id = create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id
    org = db.get(Organization, org_id)
    actor = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.role == "member")).first()
    return org, actor


def test_client_send_blocks_then_reject_reason_changes_next_draft(db):
    org, actor = _org(db)

    # attempt 1: client send is queued for approval, not sent
    r1 = g.request_outbound(db, org, actor, "client_send", "Send the proposal")
    assert r1["status"] == "pending_approval"
    first_preview = r1["preview"]

    # CEO rejects with a reason
    from app.models import ApprovalRequest
    approval = db.get(ApprovalRequest, r1["approval_id"])
    g.decide(db, org, approval, approver_user_id="ceo", decision="reject",
             reason="Add a pricing breakdown and soften the tone")

    # attempt 2: the new draft visibly carries the rejection reason
    r2 = g.request_outbound(db, org, actor, "client_send", "Send the proposal")
    assert r2["status"] == "pending_approval"
    assert r2["preview"] != first_preview
    assert "pricing breakdown" in r2["preview"]

    # approve -> sent
    approval2 = db.get(ApprovalRequest, r2["approval_id"])
    out = g.decide(db, org, approval2, approver_user_id="ceo", decision="approve", reason=None)
    assert out["status"] == "approved"


def test_forbidden_claim_is_denied_with_rule(db):
    org, actor = _org(db)
    with pytest.raises(g.Denied) as exc:
        # intent carries a forbidden claim -> deny policy fires before approval
        g.request_outbound(db, org, actor, "client_send", "we offer guaranteed returns")
    assert exc.value.rule == "forbidden-claims"


def test_kill_switch_denies_outbound(db):
    org, actor = _org(db)
    org.killed = True
    db.flush()
    with pytest.raises(g.Denied) as exc:
        g.request_outbound(db, org, actor, "client_send", "hello")
    assert exc.value.rule == "kill_switch"


def test_budget_cap_escalates(db):
    org, actor = _org(db)
    org.cost_cap_usd = 0.01
    db.add(AgentRun(org_id=org.id, actor_id=actor.id, status="succeeded", cost_usd=1.0))
    db.flush()
    # over budget: a non-client action that would normally allow is escalated instead
    dec = g.evaluate(db, org, actor, None, "internal_note", "x")
    assert dec.effect == "require_approval" and dec.rule == "budget_cap"


def test_simulation_tags_send_and_no_real_effect(db):
    org, actor = _org(db)
    org.simulation = True
    db.flush()
    # L3 autonomy so an internal action is allowed and actually "sent"
    from app.models import AgentProfile
    prof = db.get(AgentProfile, actor.agent_profile_id)
    prof.autonomy_default = "L3"
    db.flush()
    out = g.request_outbound(db, org, actor, "internal_note", "status update")
    assert out["status"] == "sent" and out["simulated"] is True
