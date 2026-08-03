import pytest
"""Run lifecycle + event replay + cost-cap enforcement, driven through the executor."""
from app.models import Actor, AgentProfile, Organization
from app.services import events, runs, tools


def _seed(db, *, provider="echo", model="echo-1", grants=("echo",), ceiling=1.0, max_turns=4):
    org = Organization(name="Acme")
    db.add(org)
    db.flush()
    tools.register_builtins(db, org.id, list(grants))
    profile = AgentProfile(
        org_id=org.id, name="A", provider=provider, model=model,
        cost_ceiling_usd=ceiling, max_turns=max_turns, tool_grants=list(grants),
    )
    db.add(profile)
    db.flush()
    actor = Actor(org_id=org.id, type="agent", agent_profile_id=profile.id)
    db.add(actor)
    db.commit()
    return org, actor


def test_echo_run_succeeds_with_replayable_trace(db):
    org, actor = _seed(db)
    run = runs.execute(db, runs.create_run(db, org.id, actor, "hello world"))

    assert run.status == "succeeded"
    assert run.result["text"] == "echo: {'text': 'hello world'}"
    assert run.turns_used == 2  # tool call, then finalize

    evs = events.trace(db, run.trace_id)
    actions = [e.action for e in evs]
    assert actions == ["run.started", "model.call", "tool.call", "model.call", "run.succeeded"]
    # ordering is monotonic
    assert [e.seq for e in evs] == sorted(e.seq for e in evs)
    # replay reconstructs stored run state, and total cost = sum of event costs
    assert events.replay_matches(db, run)
    assert abs(sum(e.cost_usd for e in evs) - run.cost_usd) < 1e-9


def test_cost_ceiling_fails_closed(db):
    # priced model + a ceiling one turn will blow past -> run fails, not truncates silently
    org, actor = _seed(db, model="echo-priced", ceiling=1e-9)
    run = runs.execute(db, runs.create_run(db, org.id, actor, "x " * 50))

    assert run.status == "failed"
    assert "cost_ceiling" in run.error
    assert events.replay_matches(db, run)


def test_ungranted_tool_refused(db):
    # the registry guard refuses a tool the agent wasn't granted, even if registered
    org, actor = _seed(db, grants=("echo",))
    with pytest.raises(tools.ToolError):
        tools.execute(db, org.id, grants=["echo"], name="get_time", args={})
