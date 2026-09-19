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
