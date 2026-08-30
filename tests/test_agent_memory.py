"""Per-agent persistent memory: written after real work, read before the next task or chat reply,
summarized instead of dumped raw once it gets long."""
import pytest
from sqlalchemy import select

from app.models import Actor, AgentProfile, MemoryRecord
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import agent_memory, planning


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def _sam(db, org_id):
    return db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.name == "Sam Sales Agent")).first()


def test_remember_and_read_round_trip(db):
    org_id = _org(db)
    sam = _sam(db, org_id)
    assert agent_memory.agent_memory_context(db, org_id, sam) == ""  # nothing yet

    agent_memory.remember_agent(db, org_id, sam.id, "Closed the Acme deal at $12k/mo.")
    db.commit()
    ctx = agent_memory.agent_memory_context(db, org_id, sam)
    assert "Closed the Acme deal" in ctx


def test_memory_is_scoped_per_agent_not_shared(db):
    org_id = _org(db)
    sam = _sam(db, org_id)
    mia = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.name == "Mia Marketing Agent")).first()
    agent_memory.remember_agent(db, org_id, sam.id, "Sam-only fact about a sales call.")
    db.commit()
    assert "Sam-only fact" in agent_memory.agent_memory_context(db, org_id, sam)
    assert "Sam-only fact" not in agent_memory.agent_memory_context(db, org_id, mia)


def test_short_memory_is_not_summarized(db):
    """Below the cap, the raw entries are shown as-is — no model call, no lossy compression for
    something that already fits."""
    org_id = _org(db)
    sam = _sam(db, org_id)
    agent_memory.remember_agent(db, org_id, sam.id, "A short entry.")
    db.commit()
    assert agent_memory.agent_memory_context(db, org_id, sam) == "- A short entry."


def test_long_memory_on_echo_falls_back_to_recent_slice_not_a_crash(db):
    """Echo has no real reasoning to summarize with — long memory must still return something
    useful (the most recent slice) instead of erroring or returning everything unclipped."""
    org_id = _org(db)
    sam = _sam(db, org_id)
    for i in range(50):
        agent_memory.remember_agent(db, org_id, sam.id, f"Entry number {i} with some padding text here.")
    db.commit()
    ctx = agent_memory.agent_memory_context(db, org_id, sam)
    assert ctx and len(ctx) <= agent_memory._RAW_CAP
    assert "Entry number 49" in ctx  # the most recent one survives the clip


def test_long_memory_on_a_real_provider_gets_summarized(db, monkeypatch):
    org_id = _org(db)
    sam = _sam(db, org_id)
    prof = db.get(AgentProfile, sam.agent_profile_id)
    prof.provider, prof.model = "mistral", "mistral-small-latest"
    for i in range(50):
        agent_memory.remember_agent(db, org_id, sam.id, f"Entry number {i} with some padding text here.")
    db.commit()

    class _FakeProvider:
        def complete(self, **kwargs):
            class C:
                text = "- Closed three deals\n- Focused on enterprise accounts"
            return C()

    monkeypatch.setattr("app.services.llm.build_provider", lambda *a, **k: _FakeProvider())
    ctx = agent_memory.agent_memory_context(db, org_id, sam)
    assert ctx == "- Closed three deals\n- Focused on enterprise accounts"


def test_summarization_error_falls_back_to_recent_slice(db, monkeypatch):
    org_id = _org(db)
    sam = _sam(db, org_id)
    prof = db.get(AgentProfile, sam.agent_profile_id)
    prof.provider, prof.model = "mistral", "mistral-small-latest"
    for i in range(50):
        agent_memory.remember_agent(db, org_id, sam.id, f"Entry number {i} with some padding text here.")
    db.commit()

    def _boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.services.llm.build_provider", _boom)
    ctx = agent_memory.agent_memory_context(db, org_id, sam)
    assert ctx and "Entry number 49" in ctx  # graceful fallback, not a crash


def test_only_this_agent_scope_is_read_not_project_scoped_memory(db):
    """Agent memory (scope="agent") must be completely separate from the existing shared
    project memory (scope="project") — reading one must never leak the other."""
    org_id = _org(db)
    sam = _sam(db, org_id)
    db.add(MemoryRecord(org_id=org_id, scope="project", source_actor_id=sam.id,
                        content="Shared project memory entry — should NOT show up here."))
    db.commit()
    assert agent_memory.agent_memory_context(db, org_id, sam) == ""


# ---------- wiring: memory actually reaches task execution and chat replies ----------

def test_gather_context_includes_the_assignees_own_memory(db):
    org_id = _org(db)
    project, tasks = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    t = tasks[0]
    agent_memory.remember_agent(db, org_id, t.assignee_actor_id, "I previously learned X about this client.")
    db.commit()
    ctx = planning._gather_context(db, project, t, {tt.id: tt for tt in tasks}, {}, {})
    assert "previously learned X" in ctx


def test_execute_project_writes_agent_memory_for_completed_tasks(db):
    org_id = _org(db)
    project, tasks = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    planning.approve_project(db, project)
    planning.execute_project(db, project)
    db.commit()
    any_agent_memory = db.scalar(select(MemoryRecord).where(
        MemoryRecord.org_id == org_id, MemoryRecord.scope == "agent"))
    assert any_agent_memory is not None


def test_rerun_task_chat_assigned_also_writes_agent_memory(db):
    """Chat-assigned work (team chat @mentions) never wrote any memory before — confirm it now
    builds the agent's own history too, not just real full-project runs."""
    from app.services import teamchat

    org_id = _org(db)
    out = teamchat.post(db, org_id, "@mia write a one-line positioning statement for a med-spa")
    db.commit()
    from app.models import Project, Task
    task = db.get(Task, out["tasks"][0]["task_id"])
    planning.rerun_task(db, db.get(Project, task.project_id), task)
    db.commit()
    mem = db.scalars(select(MemoryRecord).where(
        MemoryRecord.scope == "agent", MemoryRecord.source_actor_id == task.assignee_actor_id)).first()
    assert mem is not None
