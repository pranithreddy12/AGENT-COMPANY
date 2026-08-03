"""Named agents, ask-an-agent, and task-based communication."""
from sqlalchemy import select

from app.models import Actor, Message, Thread
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import planning, talk


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def test_agents_have_persona_names_ending_in_agent(db):
    org_id = _org(db)
    agents = list(db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.type == "agent")))
    assert len(agents) >= 8
    assert all(a.name and a.name.endswith("Agent") for a in agents)
    names = {a.name for a in agents}
    assert "Sam Sales Agent" in names and "Cora Lead Agent" in names and "Quinn QA Agent" in names


def test_ask_agent_returns_an_answer(db):
    org_id = _org(db)
    sales = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.name == "Sam Sales Agent")).first()
    ans = talk.ask_agent(db, org_id, sales, "What's your status?")
    assert isinstance(ans, str) and ans.strip()


def test_agents_post_task_based_communication(db):
    org_id = _org(db)
    project, _ = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    planning.approve_project(db, project)
    planning.execute_project(db, project)

    st = db.scalars(select(Thread).where(Thread.project_id == project.id, Thread.thread_type == "status")).first()
    assert st is not None
    msgs = [m.content for m in db.scalars(select(Message).where(Message.thread_id == st.id))]
    assert len(msgs) >= 8  # Lead kickoff + a start + done per task = a real conversation
    assert any("Plan set" in m for m in msgs)                    # the Lead opens the chat
    assert any("picking up" in m or "starting" in m for m in msgs)  # agent acknowledges before working
    assert any("done with" in m and "Agent" in m for m in msgs)     # first-person, named, reports back
    assert any("Over to you" in m for m in msgs)                    # hands to the next agent by name
