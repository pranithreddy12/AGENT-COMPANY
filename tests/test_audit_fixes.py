"""Regressions for the architecture audit: side-channel model calls are metered + governed, a broken
Critic doesn't trigger pointless revisions, chat POST never blocks on a model call, restart orphans are
recovered, and failures read as failures."""
import httpx
from sqlalchemy import select

from app.main import recover_stuck
from app.models import Actor, AgentProfile, AgentRun, Message, Organization, Task
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import cost, governance, llm, planning, runs, teamchat
from app.services.llm import Completion


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def _actor(db, org_id, name):
    return db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.name == name)).first()


def _go_real(db, actor, model="mistral-small-latest"):
    prof = db.get(AgentProfile, actor.agent_profile_id)
    prof.provider, prof.model = "mistral", model
    db.commit()
    return prof


class _Fake:
    def __init__(self, text="ok", in_tok=1000, out_tok=500):
        self.text, self.in_tok, self.out_tok, self.calls = text, in_tok, out_tok, 0

    def complete(self, **kw):
        self.calls += 1
        return Completion(text=self.text, input_tokens=self.in_tok, output_tokens=self.out_tok)


# ---------- metering: every model call is counted and governed ----------

def test_metered_call_is_recorded_and_counted_toward_org_spend(db, monkeypatch):
    org_id = _org(db)
    mia = _actor(db, org_id, "Mia Marketing Agent")
    prof = _go_real(db, mia)
    monkeypatch.setattr(llm, "build_provider", lambda *a, **k: _Fake())
    before = governance.spent(db, org_id)

    provider = runs.metered(db, org_id, mia, prof, "chat reply")
    provider.complete(system="", messages=[], tools=[], max_tokens=10)

    expected = cost.compute("mistral-small-latest", 1000, 500)
    assert expected > 0
    assert abs(governance.spent(db, org_id) - before - expected) < 1e-9  # was invisible before
    row = db.scalars(select(AgentRun).where(AgentRun.trigger == "chat reply")).first()
    assert row is not None and row.status == "succeeded"


def test_metered_call_refuses_while_the_kill_switch_is_on(db, monkeypatch):
    org_id = _org(db)
    mia = _actor(db, org_id, "Mia Marketing Agent")
    prof = _go_real(db, mia)
    fake = _Fake()
    monkeypatch.setattr(llm, "build_provider", lambda *a, **k: fake)
    db.get(Organization, org_id).killed = True
    db.commit()

    provider = runs.metered(db, org_id, mia, prof, "chat reply")
    try:
        provider.complete(system="", messages=[], tools=[], max_tokens=10)
        raised = False
    except governance.Blocked as e:
        raised = "kill switch" in str(e)
    assert raised and fake.calls == 0  # nothing spent


def test_chat_reply_says_why_when_blocked_instead_of_a_bare_type_name(db, monkeypatch):
    org_id = _org(db)
    mia = _actor(db, org_id, "Mia Marketing Agent")
    _go_real(db, mia)
    monkeypatch.setattr(llm, "build_provider", lambda *a, **k: _Fake())
    db.get(Organization, org_id).killed = True
    db.commit()
    monkeypatch.setattr(teamchat, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    teamchat.run_chat_reply_in_background(org_id, mia.id, "hello?")

    last = db.scalars(select(Message).order_by(Message.created_at.desc())).first()
    assert "kill switch" in last.content


def test_unlisted_model_is_priced_conservatively_instead_of_failing_the_run():
    assert cost.compute_or_default("some-new-model-1", 1_000_000, 0) == 15.0  # opus-class default
    assert cost.compute_or_default("echo-1", 1000, 1000) == 0.0  # known models unchanged
    try:
        cost.compute("some-new-model-1", 1, 1)
        failed_closed = False
    except cost.UnknownModelError:
        failed_closed = True
    assert failed_closed  # the strict function keeps its contract


# ---------- a broken Critic must not send good work back for revision ----------

def test_unparseable_critic_reply_escalates_without_rerunning_the_worker(db, monkeypatch):
    org_id = _org(db)
    project, tasks = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    critic = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.role == "critic")).first()
    _go_real(db, critic)
    monkeypatch.setattr(llm, "build_provider", lambda *a, **k: _Fake(text="I think it looks fine!"))

    worker_runs = {"n": 0}
    real = planning.runs.execute

    def spy(db_, run, extra_system=""):
        worker_runs["n"] += 1
        return real(db_, run, extra_system=extra_system)

    monkeypatch.setattr(planning.runs, "execute", spy)
    art = planning._run_and_review(db, project, tasks[0], critic, context="")

    assert art.needs_human and "QA review unavailable" in art.critic_reasons[0]
    assert worker_runs["n"] == 1  # the producer was NOT sent back to "fix" an un-critiqued draft


def test_critic_retries_once_with_a_stricter_instruction_before_giving_up(db, monkeypatch):
    from app.services import review

    replies = iter(["looks fine to me", '{"passed": true, "reasons": []}'])

    class _Judge:
        def complete(self, **kw):
            return Completion(text=next(replies), input_tokens=1, output_tokens=1)

    verdict = review.llm_critic_review(_Judge(), "content", "criteria", "playbook")
    assert verdict.passed and not verdict.error


# ---------- the chat POST must never wait on a model ----------

def test_deferred_post_records_the_message_and_returns_pending_without_classifying(db, monkeypatch):
    org_id = _org(db)
    sam = _actor(db, org_id, "Sam Sales Agent")
    _go_real(db, sam)

    def boom(*a, **k):
        raise AssertionError("classification is a model call - it must not run inside the request")

    monkeypatch.setattr(teamchat, "classify_intent", boom)
    out = teamchat.post(db, org_id, "@sam write a pitch", defer=True)

    assert [t["kind"] for t in out["tasks"]] == ["pending"]
    assert out["tasks"][0]["agent"] == "Sam Sales Agent"
    assert db.scalars(select(Task).where(Task.org_id == org_id)).first() is None  # no task made yet


def test_background_worker_classifies_then_creates_and_runs_the_task(db, monkeypatch):
    org_id = _org(db)
    sam = _actor(db, org_id, "Sam Sales Agent")
    monkeypatch.setattr(teamchat, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    ran = {}
    monkeypatch.setattr(teamchat, "run_chat_task_in_background", lambda task_id: ran.setdefault("task", task_id))

    teamchat.run_chat_mention_in_background(org_id, sam.id, "write a pitch")  # Echo classifies as task

    task = db.get(Task, ran["task"])
    assert task.assignee_actor_id == sam.id and task.goal == "write a pitch"


def test_background_worker_routes_a_conversational_mention_to_a_reply(db, monkeypatch):
    org_id = _org(db)
    sam = _actor(db, org_id, "Sam Sales Agent")
    monkeypatch.setattr(teamchat, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(teamchat, "classify_intent", lambda *a, **k: "chat")
    seen = {}
    monkeypatch.setattr(teamchat, "run_chat_reply_in_background", lambda o, a, g: seen.setdefault("reply", (a, g)))

    teamchat.run_chat_mention_in_background(org_id, sam.id, "how is it going?")
    assert seen["reply"] == (sam.id, "how is it going?")


# ---------- restart recovery ----------

def test_restart_blocks_orphaned_chat_tasks_and_tells_the_human(db):
    org_id = _org(db)
    sam = _actor(db, org_id, "Sam Sales Agent")
    out = teamchat.post(db, org_id, "@sam write a pitch")  # Echo -> a real in_progress Task
    db.commit()
    task = db.get(Task, out["tasks"][0]["task_id"])
    assert task.status == "in_progress"

    recover_stuck(db)  # what startup runs

    db.refresh(task)
    assert task.status == "blocked"
    last = db.scalars(select(Message).order_by(Message.created_at.desc())).first()
    assert last.sender_actor_id == sam.id and "restarted" in last.content


# ---------- failures read as failures ----------

def test_a_failed_run_is_reported_as_a_failure_not_as_done():
    art = type("A", (), {"blocked": False, "needs_human": True, "content": "model call: timed out",
                         "critic_reasons": ["run failed: model call: timed out"]})()
    s = teamchat._summary(art, request="write a pitch")
    assert s.startswith("I couldn't finish that") and "timed out" in s
    assert "Done" not in s


def test_explain_error_gives_actionable_text():
    req = httpx.Request("POST", "http://x")

    def status(code):
        return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))

    assert "API key" in llm.explain_error(status(401))
    assert "model name" in llm.explain_error(status(404))
    assert "rate limited" in llm.explain_error(status(429))
    assert "too long" in llm.explain_error(httpx.ReadTimeout("t"))
    assert "Ollama" in llm.explain_error(httpx.ConnectError("refused"))
    assert llm.explain_error(ValueError("bad")) == "ValueError: bad"


# ---------- external client accounts are not staff ----------

def test_client_role_cannot_reach_internal_endpoints_but_staff_can():
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.auth import issue_token
    from app.db import Base, get_db
    from app.main import app
    from app.models import Account, User

    # StaticPool: TestClient serves requests on worker threads, and a plain in-memory SQLite gives
    # every thread its own empty database
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
        acct = Account(org_id=r.org_id, name="Client Inc", is_client=True)
        seed.add(acct)
        seed.flush()
        client_user = User(org_id=r.org_id, email="cl@x.com", pw_hash="x", role="client", account_id=acct.id)
        seed.add(client_user)
        seed.commit()
        c = TestClient(app)
        ch = {"authorization": f"Bearer {issue_token(client_user)}"}
        sh = {"authorization": f"Bearer {r.access_token}"}

        for path in ("/projects", "/departments", "/teamchat", "/approvals"):
            assert c.get(path, headers=ch).status_code == 403, path  # was 200: internal data leaked
            assert c.get(path, headers=sh).status_code == 200, path
        assert c.post("/runs", headers=ch, json={"actor_id": "x", "input": "hi"}).status_code == 403
        assert c.get("/portal/projects", headers=ch).status_code == 200  # the portal still works
    finally:
        app.dependency_overrides.clear()


# ---------- execution semantics: resume, one artifact per task, Legal on chat work ----------

def _run_all(db, org_id):
    project, tasks = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    planning.approve_project(db, project)
    planning.execute_project(db, project)
    return project, tasks


def _count_worker_runs(monkeypatch):
    n = {"runs": 0}
    real = planning.runs.execute

    def spy(db_, run, extra_system=""):
        n["runs"] += 1
        return real(db_, run, extra_system=extra_system)

    monkeypatch.setattr(planning.runs, "execute", spy)
    return n


def test_rerunning_a_finished_project_does_not_redo_or_rebill_any_task(db, monkeypatch):
    from app.models import Artifact

    org_id = _org(db)
    project, tasks = _run_all(db, org_id)
    before = {a.task_id: (a.id, a.version) for a in db.scalars(select(Artifact))}
    n = _count_worker_runs(monkeypatch)

    planning.execute_project(db, project)  # "Run" pressed again

    assert n["runs"] == 0  # every task was already done: nothing re-executed
    assert {a.task_id: (a.id, a.version) for a in db.scalars(select(Artifact))} == before


def test_resume_reruns_only_the_unfinished_task_and_keeps_one_artifact_per_task(db, monkeypatch):
    from app.models import Artifact

    org_id = _org(db)
    project, tasks = _run_all(db, org_id)
    victim = tasks[2]
    art = db.scalars(select(Artifact).where(Artifact.task_id == victim.id)).first()
    victim.status, art.needs_human = "blocked", True  # e.g. it failed last time
    db.commit()
    n = _count_worker_runs(monkeypatch)

    planning.execute_project(db, project)

    assert n["runs"] == 1  # only the blocked task ran again
    arts = list(db.scalars(select(Artifact).where(Artifact.task_id == victim.id)))
    assert len(arts) == 1 and arts[0].version >= 2  # same row, version bumped - no stale duplicate
    assert not arts[0].needs_human


def test_a_paused_run_stops_cleanly_and_says_so(db, monkeypatch):
    org_id = _org(db)
    project, tasks = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    planning.approve_project(db, project)
    db.get(Organization, org_id).killed = True
    db.commit()
    n = _count_worker_runs(monkeypatch)

    planning.execute_project(db, project)

    assert n["runs"] == 0
    msgs = [m.content for m in db.scalars(select(Message))]
    assert any("Run paused" in m and "kill switch" in m for m in msgs)
    assert all(t.status != "done" for t in tasks)  # and nothing pretends to be finished


def test_chat_assigned_work_gets_the_legal_screen(db):
    org_id = _org(db)
    out = teamchat.post(db, org_id, "@sam write copy promising guaranteed returns to investors")
    db.commit()
    task = db.get(Task, out["tasks"][0]["task_id"])
    project = db.get(planning.Project, task.project_id)

    art = planning.rerun_task(db, project, task)

    assert art.blocked and "guaranteed returns" in art.block_reason
    assert task.status == "blocked"
    assert teamchat._summary(art, request=task.goal).startswith("Legal blocked this")


# ---------- plan sanity ----------

def test_absurd_model_effort_estimates_are_clamped_before_scheduling(db, monkeypatch):
    org_id = _org(db)

    class _P:
        def plan(self, *, goal, departments, max_tokens):
            from app.services.llm import PlanResult
            return PlanResult(tasks=[
                {"temp_id": "a", "goal": "zero", "department": "Sales", "est_effort_hours": 0, "depends_on": []},
                {"temp_id": "b", "goal": "neg", "department": "Sales", "est_effort_hours": -5, "depends_on": ["a"]},
                {"temp_id": "c", "goal": "huge", "department": "Sales", "est_effort_hours": 9999, "depends_on": ["b"]},
            ], input_tokens=1, output_tokens=1)

    monkeypatch.setattr(planning, "build_provider", lambda *a, **k: _P())
    _project, tasks = planning.draft_project(db, org_id, "goal")
    assert [t.est_effort_hours for t in tasks] == [0.25, 0.25, 200.0]


def test_a_plan_with_too_many_tasks_is_rejected_so_the_model_replans_smaller():
    import pytest

    from app.services.llm import MAX_PLAN_TASKS, PlanParseError, validate_plan

    many = [{"temp_id": f"t{i}", "goal": "g", "department": "Sales", "est_effort_hours": 1, "depends_on": []}
            for i in range(MAX_PLAN_TASKS + 1)]
    with pytest.raises(PlanParseError, match="at most"):
        validate_plan({"tasks": many})


# ---------- agents know the company they work for ----------

def test_every_task_brief_names_the_company_and_carries_its_profile(db, monkeypatch):
    org_id = _org(db)  # org is named "Acme"
    db.get(Organization, org_id).profile = "We sell Instagram lead-gen to med-spas from $1.2k/month."
    db.commit()
    project, tasks = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    planning.approve_project(db, project)
    seen = []
    real = planning.runs.execute

    def spy(db_, run, extra_system=""):
        seen.append(run.trigger)
        return real(db_, run, extra_system=extra_system)

    monkeypatch.setattr(planning.runs, "execute", spy)
    planning.execute_project(db, project)

    assert seen and all("Acme" in t and "med-spas from $1.2k/month" in t for t in seen)
    assert all("never [Company Name]" in t for t in seen)  # and the instruction not to placeholder it


def test_company_context_works_without_a_profile_and_bounds_a_huge_one(db):
    from app.services import company

    org_id = _org(db)
    assert "Acme" in company.company_context(db, org_id)  # the name alone removes [Company Name]
    db.get(Organization, org_id).profile = "x" * 50_000
    db.commit()
    assert len(company.company_context(db, org_id)) < company.PROFILE_MAX_CHARS + 500  # bounded in prompts
    assert company.company_context(db, "no-such-org") == ""


def test_chat_replies_are_told_the_company(db, monkeypatch):
    org_id = _org(db)
    db.get(Organization, org_id).profile = "Boutique consultancy for dentists."
    mia = _actor(db, org_id, "Mia Marketing Agent")
    _go_real(db, mia)
    seen = {}

    class _P:
        def complete(self, **kw):
            seen["system"] = kw["system"]
            return Completion(text="hi", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(llm, "build_provider", lambda *a, **k: _P())
    monkeypatch.setattr(teamchat, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    teamchat.run_chat_reply_in_background(org_id, mia.id, "what do we do?")
    assert "Acme" in seen["system"] and "Boutique consultancy for dentists." in seen["system"]


def test_profile_api_roundtrip_is_ceo_only_and_length_bounded():
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.auth import issue_token
    from app.db import Base, get_db
    from app.main import app
    from app.models import User

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
        member = User(org_id=r.org_id, email="m@a.com", pw_hash="x", role="member")
        seed.add(member)
        seed.commit()
        c = TestClient(app)
        ceo = {"authorization": f"Bearer {r.access_token}"}
        mem = {"authorization": f"Bearer {issue_token(member)}"}

        assert c.post("/settings/profile", headers=ceo, json={"profile": "We sell widgets."}).json()["saved"]
        got = c.get("/settings/profile", headers=mem).json()  # any staff can read it
        assert got["profile"] == "We sell widgets." and got["name"] == "Http Co"
        assert c.post("/settings/profile", headers=mem, json={"profile": "hax"}).status_code == 403
        assert c.post("/settings/profile", headers=ceo, json={"profile": "x" * 4001}).status_code == 422
    finally:
        app.dependency_overrides.clear()
