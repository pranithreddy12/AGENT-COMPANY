"""services/validate.py - deterministic checks - and its wiring into task review and proposals."""
from datetime import date

from sqlalchemy import select

from app.models import Actor, AgentProfile, Organization
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import llm, planning, validate
from app.services.llm import Completion

TODAY = date(2026, 10, 23)
PRICES = "Starter: $1,200/month\nGrowth: $2,500/month\nAudit: $900 one-off"


def kinds(text, **kw):
    return [i.kind for i in validate.check(text, today=TODAY, **kw)]


# ---------- placeholders ----------

def test_bracket_placeholders_are_flagged_with_a_specific_fix():
    issues = validate.check("Dear [Client Name], welcome to [Company Name].", today=TODAY)
    assert [i.detail for i in issues] == ["[Client Name]", "[Company Name]"]
    assert all("Replace" in i.fix for i in issues)


def test_repeated_placeholder_is_reported_once():
    assert len(validate.check("[Date] and again [Date]", today=TODAY)) == 1


def test_sanctioned_price_marker_links_and_checkboxes_are_not_placeholders():
    ok = "Investment: [price to confirm]. See [our site](https://x.com). [ ] todo [x] done"
    assert kinds(ok) == []


def test_filler_text_is_flagged():
    assert "filler" in kinds("Lorem ipsum dolor sit amet")


# ---------- prices ----------

def test_amount_not_in_the_price_list_is_flagged():
    assert kinds("We charge $1,500 per month.", price_list=PRICES) == ["price"]


def test_exact_listed_amounts_pass_in_any_notation():
    assert kinds("Starter at $1,200 a month, or 1200 USD.", price_list=PRICES) == []
    assert kinds("Growth is $2.5k/month; audit USD 900.", price_list=PRICES) == []


def test_a_sum_is_a_new_unapproved_number():
    assert kinds("Starter + audit = $2,100 in month one.", price_list=PRICES) == ["price"]


def test_no_price_list_flags_amounts_only_when_required():
    assert kinds("It costs $500.") == []  # an internal task may mention numbers freely
    assert kinds("It costs $500.", require_price_list=True) == ["price"]  # a proposal may not


def test_non_money_numbers_are_ignored():
    assert kinds("Grow followers 20% in 90 days with 3 posts a week.", price_list=PRICES, require_price_list=True) == []


# ---------- dates ----------

def test_a_past_year_used_as_a_deadline_is_flagged():
    assert kinds("Launch by 15 March 2023 and deliver in Q4.") == ["date"]


def test_a_past_year_as_history_is_not_flagged():
    assert kinds("Founded in 2019; our 2023 case study doubled leads.") == []


def test_current_and_future_years_are_fine():
    assert kinds("Launch by 15 March 2027, kickoff in November 2026.") == []


# ---------- feedback ----------

def test_feedback_is_numbered_and_specific():
    fb = validate.as_feedback(validate.check("[Name] pays $9.", price_list=PRICES, today=TODAY))
    assert fb.startswith("Automated checks failed") and "1. [Name]" in fb and "2. amount(s)" in fb


# ---------- wiring: a real-provider task draft is checked before the Critic is paid ----------

def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def test_task_draft_with_a_placeholder_is_sent_back_with_exact_fixes_and_skips_the_critic(db, monkeypatch):
    org_id = _org(db)
    project, tasks = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    task = tasks[0]
    worker = db.get(Actor, task.assignee_actor_id)
    prof = db.get(AgentProfile, worker.agent_profile_id)
    prof.provider, prof.model = "mistral", "mistral-small-latest"
    db.commit()

    drafts = iter(["Hi [Client Name], here is the plan.", "Hi Acme, here is the plan."])
    seen_briefs = []

    class _P:
        def complete(self, **kw):
            seen_briefs.append(kw["messages"][-1]["content"])
            return Completion(text=next(drafts), input_tokens=1, output_tokens=1)

    critic_calls = {"n": 0}

    def fake_review(*a, **k):
        critic_calls["n"] += 1
        from app.services.review import Verdict
        return Verdict(True, [])

    monkeypatch.setattr(planning.runs, "build_provider", lambda *a, **k: _P())
    monkeypatch.setattr(planning, "_review", fake_review)
    critic = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.role == "critic")).first()

    art = planning._run_and_review(db, project, task, critic, context="")

    assert art.content == "Hi Acme, here is the plan." and not art.needs_human
    assert "Automated checks failed" in seen_briefs[1] and "[Client Name]" in seen_briefs[1]  # targeted retry
    assert critic_calls["n"] == 1  # only the clean draft ever reached the paid Critic


def test_echo_drafts_are_never_validated(db):
    org_id = _org(db)
    worker = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.name == "Sam Sales Agent")).first()
    assert planning._draft_issues(db, org_id, worker, "[Company Name] $999") == []


# ---------- wiring: proposals ----------

class _HF:
    company, industry, location, context, contact_name, contact_email = "Dental Co", "dental", "Dubai", "", None, None
    goal, leadforge_lead_id = None, "lf-1"

    class _S:
        signal, source = "slow follow-up", "web"

    signals = [_S()]


def _proposal_org(db, price_list=None):
    org_id = _org(db)
    sam = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.name == "Sam Sales Agent")).first()
    prof = db.get(AgentProfile, sam.agent_profile_id)
    prof.provider, prof.model = "mistral", "mistral-small-latest"
    db.get(Organization, org_id).price_list = price_list
    db.commit()
    return org_id


def _scripted(monkeypatch, replies):
    seq = iter(replies)
    calls = []

    class _P:
        def complete(self, **kw):
            calls.append(kw["messages"])
            return Completion(text=next(seq), input_tokens=1, output_tokens=1)

    monkeypatch.setattr(llm, "build_provider", lambda *a, **k: _P())
    return calls


def test_proposal_with_an_invented_price_gets_one_targeted_retry_then_passes(db, monkeypatch):
    from app.services import integrations

    org_id = _proposal_org(db, PRICES)
    calls = _scripted(monkeypatch, ["Investment: $4,000.", "Investment: Growth at $2,500/month."])
    out = integrations.generate_proposal(db, org_id, _HF())

    assert "Growth at $2,500/month" in out["proposal"]
    assert len(calls) == 2 and "Automated checks failed" in calls[1][-1]["content"]
    art = integrations._proposal_artifact(db, db.get(planning.Project, out["project_id"]))
    assert art.critic_reasons is None  # clean after the retry


def test_proposal_that_still_fails_is_kept_but_flagged_with_the_reason(db, monkeypatch):
    from app.services import integrations

    org_id = _proposal_org(db, PRICES)
    calls = _scripted(monkeypatch, ["Investment: $4,000.", "Investment: $4,500."])
    out = integrations.generate_proposal(db, org_id, _HF())

    art = integrations._proposal_artifact(db, db.get(planning.Project, out["project_id"]))
    assert len(calls) == 2  # capped at ONE retry
    assert art.needs_human and any(r.startswith("price:") for r in art.critic_reasons)


def test_proposal_without_a_price_list_may_not_quote_a_number(db, monkeypatch):
    from app.services import integrations

    org_id = _proposal_org(db, None)
    _scripted(monkeypatch, ["Investment: $1,000.", "Investment: [price to confirm]."])
    out = integrations.generate_proposal(db, org_id, _HF())
    assert "[price to confirm]" in out["proposal"]


# ---------- the price list reaches the agents ----------

def test_company_context_carries_the_price_list_or_the_marker_instruction(db):
    from app.services import company

    org_id = _org(db)
    assert "[price to confirm]" in company.company_context(db, org_id)
    db.get(Organization, org_id).price_list = PRICES
    db.commit()
    ctx = company.company_context(db, org_id)
    assert "$1,200/month" in ctx and "ONLY these amounts" in ctx


# ---------- readiness checklist ----------

def test_readiness_reports_what_is_missing(db):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base, get_db
    from app.main import app

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def _override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    try:
        seed = Session()
        r = create_org(OrgCreate(name="Http Co", ceo_email="ceo@a.com", ceo_password="pw"), seed)
        c = TestClient(app)
        h = {"authorization": f"Bearer {r.access_token}"}
        got = c.get("/console/readiness", headers=h).json()
        assert got["ready"] is False and [i["key"] for i in got["items"] if not i["done"]] == ["model", "profile", "prices"]

        c.post("/settings/profile", headers=h, json={"profile": "We sell X.", "price_list": PRICES})
        c.post("/settings/llm", headers=h, json={"provider": "ollama", "model": "qwen2.5:7b"})
        assert c.get("/console/readiness", headers=h).json()["ready"] is True
        assert c.get("/settings/profile", headers=h).json()["price_list"] == PRICES
    finally:
        app.dependency_overrides.clear()
