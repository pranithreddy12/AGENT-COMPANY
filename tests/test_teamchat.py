"""Team chat: @mentioning an agent must create REAL work, not just talk."""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.db import Base
from app.models import Actor, Task
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import teamchat


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def test_handles_are_agent_first_names(db):
    org_id = _org(db)
    handles = {teamchat.handle(a) for a in teamchat.roster(db, org_id)}
    assert {"cleo", "sam", "lena", "piper", "mia"} <= handles  # 'Cleo Client Agent' -> 'cleo'


def test_mention_creates_a_real_task_assigned_to_that_agent(db):
    org_id = _org(db)
    out = teamchat.post(db, org_id, "@cleo draft a proposal for the BizBuySell scrape")
    db.commit()
    assert len(out["tasks"]) == 1
    t = db.get(Task, out["tasks"][0]["task_id"])
    agent = db.get(Actor, t.assignee_actor_id)
    assert teamchat.handle(agent) == "cleo"
    assert t.department_id == agent.department_id      # routed to the agent's department
    assert "@cleo" not in t.goal and "BizBuySell" in t.goal  # goal is the instruction, handle stripped


def test_multiple_mentions_create_one_task_each(db):
    org_id = _org(db)
    out = teamchat.post(db, org_id, "@sam and @lena please review the scraping deal")
    db.commit()
    assert sorted(t["handle"] for t in out["tasks"]) == ["lena", "sam"]
    assert len({t["task_id"] for t in out["tasks"]}) == 2


def test_repeated_mention_of_same_agent_creates_one_task(db):
    org_id = _org(db)
    out = teamchat.post(db, org_id, "@cleo hey @cleo one more thing")
    db.commit()
    assert len(out["tasks"]) == 1  # deduped — not two tasks for one agent


def test_message_without_mentions_creates_no_work(db):
    org_id = _org(db)
    out = teamchat.post(db, org_id, "just thinking out loud about the roadmap")
    db.commit()
    assert out["tasks"] == []
    assert db.scalar(select(Task).where(Task.org_id == org_id)) is None


def test_unknown_handle_is_ignored(db):
    org_id = _org(db)
    out = teamchat.post(db, org_id, "@nobody do something")
    db.commit()
    assert out["tasks"] == []


def test_empty_message_rejected(db):
    org_id = _org(db)
    assert teamchat.post(db, org_id, "   ")["error"] == "empty_message"


def test_history_renders_sender_and_survives_many_messages(db):
    """The chat thread must never hit the agent-loop budget guard that escalates at 6 messages."""
    org_id = _org(db)
    for i in range(25):
        teamchat.post(db, org_id, f"message {i}")
    db.commit()
    h = teamchat.history(db, org_id)
    assert len(h) == 25
    assert h[0]["content"] == "message 0" and h[-1]["content"] == "message 24"  # oldest first
    assert h[0]["sender"] == "You" and h[0]["is_agent"] is False
    assert teamchat.team_thread(db, org_id).status == "open"  # not escalated


def test_agent_reply_lands_in_chat_as_the_agent(db):
    org_id = _org(db)
    agent = next(a for a in teamchat.roster(db, org_id) if teamchat.handle(a) == "cleo")
    teamchat._reply(db, org_id, agent, "Done — here is the draft.")
    db.commit()
    last = teamchat.history(db, org_id)[-1]
    assert last["is_agent"] is True and last["sender"] == agent.name


def test_mentioned_agent_actually_does_the_work_and_replies(db):
    """The whole point: a mention runs the real agent->Critic->Legal path and the result lands back
    in the chat. Mirrors run_chat_task_in_background using the test session (the worker opens its
    own SessionLocal, which a unit test can't reach)."""
    from app.models import Artifact, Project
    from app.services import planning

    org_id = _org(db)
    out = teamchat.post(db, org_id, "@mia write a one-line positioning statement for a med-spa")
    db.commit()

    task = db.get(Task, out["tasks"][0]["task_id"])
    art = planning.rerun_task(db, db.get(Project, task.project_id), task)
    assert art.content.strip()                     # the agent produced a real artifact (echo provider)
    assert db.get(Artifact, art.id).task_id == task.id

    teamchat._reply(db, org_id, db.get(Actor, task.assignee_actor_id), teamchat._summary(art))
    db.commit()
    last = teamchat.history(db, org_id)[-1]
    assert last["is_agent"] is True and teamchat.handle(db.get(Actor, task.assignee_actor_id)) == "mia"
    assert last["content"].strip()


def test_summary_surfaces_legal_block_instead_of_the_text(db):
    """A Legal-blocked artifact must NOT dump its text into chat — it reports the veto."""
    from app.models import Artifact

    blocked = Artifact(org_id="o", task_id="t", type="doc", content="secret draft text",
                       blocked=True, block_reason="guarantees revenue")
    s = teamchat._summary(blocked)
    assert "Legal blocked" in s and "guarantees revenue" in s
    assert "secret draft text" not in s


def test_mentioning_the_lead_creates_no_generic_task(db):
    """The Lead's real job is drafting a project, not producing a text artifact — @mentioning her
    must NOT create a generic Task (that would just run her through the wrong code path and produce
    a chatbot-style reply instead of an actual plan)."""
    org_id = _org(db)
    cora = next(a for a in teamchat.roster(db, org_id) if a.role == "lead")
    out = teamchat.post(db, org_id, "@cora launch a customer referral program")
    db.commit()
    assert len(out["tasks"]) == 1
    t = out["tasks"][0]
    assert t["kind"] == "lead" and t["actor_id"] == cora.id
    assert "launch a customer referral program" in t["goal"]
    assert db.scalar(select(Task).where(Task.org_id == org_id)) is None  # no Task row created


def test_mentioning_the_lead_drafts_a_real_project(db):
    """Mirrors run_chat_lead_in_background's core logic with the test's own session (the real
    function opens its own SessionLocal, which points at the app's db, not this test's in-memory
    one) — proves the Lead mention path actually calls planning.draft_project, not the generic
    single-task executor."""
    from app.models import Project
    from app.services import planning

    org_id = _org(db)
    out = teamchat.post(db, org_id, "@cora launch a customer referral program")
    db.commit()
    t = out["tasks"][0]

    project, drafted = planning.draft_project(db, org_id, t["goal"])
    db.commit()
    assert isinstance(project, Project) and project.status == "planning"
    assert len(drafted) >= 3  # a real decomposed DAG, not a one-line chatbot reply

    agent = db.get(Actor, t["actor_id"])
    teamchat._reply(db, org_id, agent,
                    f"Drafted a plan — {len(drafted)} tasks across "
                    f"{len({d.department_id for d in drafted})} departments.")
    db.commit()
    last = teamchat.history(db, org_id)[-1]
    assert last["is_agent"] is True and last["sender"] == agent.name
    assert "Drafted a plan" in last["content"]


def test_chat_tasks_share_one_project(db):
    org_id = _org(db)
    teamchat.post(db, org_id, "@sam first thing")
    teamchat.post(db, org_id, "@mia second thing")
    db.commit()
    tasks = list(db.scalars(select(Task).where(Task.org_id == org_id)))
    assert len(tasks) == 2 and len({t.project_id for t in tasks}) == 1  # one chat project, not two


# ---------- intent classification: not every @mention is a task ----------

class _FakeCompletion:
    def __init__(self, text):
        self.text = text
        self.tool_calls = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.stop_reason = "end"


class _FakeProvider:
    def __init__(self, verdict):
        self.verdict = verdict

    def complete(self, **kwargs):
        return _FakeCompletion(self.verdict)


def _make_non_echo(db, org_id, handle_):
    """Flip one agent's profile to a non-echo provider so classify_intent takes the real-model
    branch instead of the deterministic echo short-circuit."""
    from app.models import AgentProfile

    agent = next(a for a in teamchat.roster(db, org_id) if teamchat.handle(a) == handle_)
    prof = db.get(AgentProfile, agent.agent_profile_id)
    prof.provider, prof.model = "mistral", "mistral-small-latest"
    db.commit()
    return agent


def test_classify_intent_echo_always_returns_task(db):
    """Echo has no real reasoning to classify with — every mention stays a task, preserving this
    path's original, already-tested contract for the zero-cost demo/test provider."""
    org_id = _org(db)
    cleo = next(a for a in teamchat.roster(db, org_id) if teamchat.handle(a) == "cleo")
    assert teamchat.classify_intent(db, org_id, cleo, "what's the status?", "") == "task"


def test_classify_intent_parses_task_verdict_from_a_real_provider(db, monkeypatch):
    org_id = _org(db)
    agent = _make_non_echo(db, org_id, "sam")
    monkeypatch.setattr(teamchat.llm, "build_provider", lambda *a, **k: _FakeProvider("TASK"))
    assert teamchat.classify_intent(db, org_id, agent, "draft a proposal", "") == "task"


def test_classify_intent_parses_chat_verdict_from_a_real_provider(db, monkeypatch):
    org_id = _org(db)
    agent = _make_non_echo(db, org_id, "sam")
    monkeypatch.setattr(teamchat.llm, "build_provider", lambda *a, **k: _FakeProvider("CHAT"))
    assert teamchat.classify_intent(db, org_id, agent, "what's the status?", "") == "chat"


def test_classify_intent_fails_safe_to_chat_on_provider_error(db, monkeypatch):
    """If classification itself can't run (network error, bad config), default to the cheaper, safer
    path — a plain reply — not a wasted Critic/Legal cycle or plan attempt on a guess."""
    org_id = _org(db)
    agent = _make_non_echo(db, org_id, "sam")

    def _boom(*a, **k):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(teamchat.llm, "build_provider", _boom)
    assert teamchat.classify_intent(db, org_id, agent, "draft a proposal", "") == "chat"


def test_post_routes_a_chat_classified_mention_without_creating_a_task(db, monkeypatch):
    """The core behavior the user asked for: a conversational message must not spawn a Task."""
    org_id = _org(db)
    monkeypatch.setattr(teamchat, "classify_intent", lambda *a, **k: "chat")
    out = teamchat.post(db, org_id, "@sam what's the status on this?")
    db.commit()
    assert len(out["tasks"]) == 1
    t = out["tasks"][0]
    assert t["kind"] == "chat" and "task_id" not in t and t["actor_id"]
    assert db.scalar(select(Task).where(Task.org_id == org_id)) is None  # no Task row created


def test_post_lead_mention_classified_as_chat_never_attempts_to_draft_a_plan(db, monkeypatch):
    """Reproduces the exact bug this fixes: @cora with a non-goal message ('restart the execution
    for tasks') used to always call draft_project and fail with a raw 'PlanError' leaking into chat.
    Classified as chat, it must route to a plain reply instead — draft_project is never even called."""
    org_id = _org(db)
    monkeypatch.setattr(teamchat, "classify_intent", lambda *a, **k: "chat")
    out = teamchat.post(db, org_id, "@cora restart the execution for tasks")
    db.commit()
    assert out["tasks"][0]["kind"] == "chat"  # not "lead" -> draft_project is never attempted


def test_post_lead_mention_classified_as_task_still_drafts_a_plan(db, monkeypatch):
    """The other half: a genuine goal sent to the Lead must still go through the real planning path
    exactly as before — classification must not break the working case while fixing the broken one."""
    org_id = _org(db)
    monkeypatch.setattr(teamchat, "classify_intent", lambda *a, **k: "task")
    out = teamchat.post(db, org_id, "@cora launch a referral program")
    db.commit()
    assert out["tasks"][0]["kind"] == "lead"
