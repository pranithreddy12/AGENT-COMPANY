"""Agent-run reliability: a failing tool must not kill the run, the kill switch / pause / budget cap
must actually stop agents, transient provider errors retry, and a model that keeps reaching for
tools is forced to produce a final answer."""
import httpx
import pytest
from sqlalchemy import select

from app.models import Actor, AgentProfile, AgentRun, Department, Organization
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import governance, llm, runs
from app.services.llm import Completion, ToolCall


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def _sam(db, org_id, provider="mistral"):
    sam = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.name == "Sam Sales Agent")).first()
    prof = db.get(AgentProfile, sam.agent_profile_id)
    prof.provider, prof.model = provider, "mistral-small-latest"
    db.commit()
    return sam


class _Scripted:
    """Provider that plays back a list of Completions (or raises exceptions), recording each call."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def complete(self, *, system, messages, tools, max_tokens):
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _use(monkeypatch, provider):
    monkeypatch.setattr(runs, "build_provider", lambda *a, **k: provider)
    monkeypatch.setattr(runs.settings, "serper_api_key", "k")


def _tool_call(name="web_search"):
    return Completion(text=None, tool_calls=[ToolCall(id="c1", name=name, args={"query": "x"})],
                      stop_reason="tool_use", input_tokens=1, output_tokens=1)


def _text(t="Final deliverable."):
    return Completion(text=t, input_tokens=1, output_tokens=1)


# ---------- a tool error is information for the model, not the end of the run ----------

def test_a_failing_tool_is_reported_to_the_model_and_the_run_still_succeeds(db, monkeypatch):
    org_id = _org(db)
    sam = _sam(db, org_id)
    prov = _Scripted([_tool_call(), _text("Answer without the search.")])
    _use(monkeypatch, prov)

    def boom(args):
        raise RuntimeError("SERPER_API_KEY not set")

    monkeypatch.setitem(runs.tools.BUILTINS["web_search"], "fn", boom)
    run = runs.execute(db, runs.create_run(db, org_id, sam, "research pricing"))

    assert run.status == "succeeded" and run.result["text"] == "Answer without the search."
    tool_msg = [m for m in prov.calls[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "FAILED" in tool_msg and "SERPER_API_KEY not set" in tool_msg


def test_web_search_is_not_offered_when_no_serper_key(db, monkeypatch):
    org_id = _org(db)
    sam = _sam(db, org_id)
    prov = _Scripted([_text()])
    monkeypatch.setattr(runs, "build_provider", lambda *a, **k: prov)
    monkeypatch.setattr(runs.settings, "serper_api_key", None)
    runs.execute(db, runs.create_run(db, org_id, sam, "hi"))
    assert "web_search" not in [t["name"] for t in prov.calls[0]["tools"]]


# ---------- the last turn takes the tools away and asks for the answer ----------

def test_last_turn_removes_tools_so_the_run_ends_with_a_deliverable(db, monkeypatch):
    org_id = _org(db)
    sam = _sam(db, org_id)
    prof = db.get(AgentProfile, sam.agent_profile_id)
    prof.max_turns = 3
    db.commit()
    prov = _Scripted([_tool_call(), _tool_call(), _text("Wrapped up.")])
    _use(monkeypatch, prov)
    monkeypatch.setitem(runs.tools.BUILTINS["web_search"], "fn", lambda a: {"results": []})
    run = runs.execute(db, runs.create_run(db, org_id, sam, "go"))

    assert run.status == "succeeded" and run.result["text"] == "Wrapped up."
    assert prov.calls[0]["tools"] and prov.calls[1]["tools"]  # tools available while there is budget
    assert prov.calls[2]["tools"] == []  # ...and gone on the final turn
    assert "final deliverable" in prov.calls[2]["messages"][-1]["content"]


def test_an_empty_model_reply_fails_cleanly_instead_of_crashing_the_artifact_write(db, monkeypatch):
    org_id = _org(db)
    sam = _sam(db, org_id)
    _use(monkeypatch, _Scripted([Completion(text=None, input_tokens=1, output_tokens=0)]))
    run = runs.execute(db, runs.create_run(db, org_id, sam, "hi"))
    assert run.status == "failed" and "empty response" in run.error


# ---------- governance actually stops agents ----------

def test_kill_switch_stops_an_agent_run_before_any_model_call(db, monkeypatch):
    org_id = _org(db)
    sam = _sam(db, org_id)
    prov = _Scripted([_text()])
    _use(monkeypatch, prov)
    db.get(Organization, org_id).killed = True
    db.commit()
    run = runs.execute(db, runs.create_run(db, org_id, sam, "hi"))
    assert run.status == "failed" and "kill switch" in run.error
    assert prov.calls == []  # no spend


def test_paused_department_stops_its_agents(db, monkeypatch):
    org_id = _org(db)
    sam = _sam(db, org_id)
    prov = _Scripted([_text()])
    _use(monkeypatch, prov)
    db.get(Department, sam.department_id).paused = True
    db.commit()
    run = runs.execute(db, runs.create_run(db, org_id, sam, "hi"))
    assert run.status == "failed" and "paused" in run.error and prov.calls == []


def test_exhausted_org_budget_stops_agents(db, monkeypatch):
    org_id = _org(db)
    sam = _sam(db, org_id)
    prov = _Scripted([_text()])
    _use(monkeypatch, prov)
    db.get(Organization, org_id).cost_cap_usd = 0.5
    db.add(AgentRun(org_id=org_id, actor_id=sam.id, status="succeeded", cost_usd=1.0))
    db.commit()
    run = runs.execute(db, runs.create_run(db, org_id, sam, "hi"))
    assert run.status == "failed" and "budget cap" in run.error and prov.calls == []


def test_run_block_reason_is_none_for_a_healthy_org(db):
    org_id = _org(db)
    assert governance.run_block_reason(db, org_id) is None


# ---------- transient provider errors retry ----------

def _status_error(code):
    req = httpx.Request("POST", "http://x")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


def test_complete_with_retry_retries_a_rate_limit_then_succeeds():
    prov = _Scripted([_status_error(429), _text("ok")])
    out = llm.complete_with_retry(prov, base_delay=0, system="", messages=[], tools=[], max_tokens=5)
    assert out.text == "ok" and len(prov.calls) == 2


def test_complete_with_retry_does_not_retry_a_client_error():
    prov = _Scripted([_status_error(400), _text("never reached")])
    with pytest.raises(httpx.HTTPStatusError):
        llm.complete_with_retry(prov, base_delay=0, system="", messages=[], tools=[], max_tokens=5)
    assert len(prov.calls) == 1


def test_complete_with_retry_does_not_retry_a_read_timeout():
    """On a slow local model a read timeout means 'still thinking' — retrying doubles the wait."""
    prov = _Scripted([httpx.ReadTimeout("slow"), _text("never reached")])
    with pytest.raises(httpx.ReadTimeout):
        llm.complete_with_retry(prov, base_delay=0, system="", messages=[], tools=[], max_tokens=5)
    assert len(prov.calls) == 1


def test_complete_with_retry_gives_up_after_the_attempt_budget():
    prov = _Scripted([_status_error(503)] * 3)
    with pytest.raises(httpx.HTTPStatusError):
        llm.complete_with_retry(prov, base_delay=0, system="", messages=[], tools=[], max_tokens=5)
    assert len(prov.calls) == 3
