"""Agents get real internet access: OpenAI-compatible providers (Mistral/OpenRouter/Ollama) now
actually send tools to the model and parse a tool call back — previously they silently ignored
`tools` entirely and always returned tool_calls=[], so a granted tool could never be invoked except
on Anthropic. Plus web_search itself: registered, wired to real Serper, granted broadly."""
import pytest
from sqlalchemy import select

from app.models import AgentProfile, Organization, ToolRegistration
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import llm, tools


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


# ---------- provider-level tool calling ----------

def test_ollama_provider_sends_tools_and_parses_a_tool_call(monkeypatch):
    """Reproduces the actual bug: before this fix, `tools` was silently dropped and tool_calls was
    always []. Mock the HTTP layer and prove both directions — the request carries the tool
    definitions, and a tool_call in the response comes back as a real ToolCall with parsed args."""
    import httpx

    sent = {}

    def fake_post(url, json, timeout, headers):
        sent["payload"] = json
        body = {
            "choices": [{"message": {
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "web_search", "arguments": '{"query": "med spa Dubai", "num": 3}'}}],
            }}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    p = llm.OllamaProvider(model="qwen2.5:7b", base_url="http://localhost:11434")
    tool_specs = [{"name": "web_search", "description": "search the web",
                  "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}}}]
    comp = p.complete(system="", messages=[{"role": "user", "content": "find med spas in Dubai"}],
                      tools=tool_specs, max_tokens=200)

    # the request actually advertised the tool to the model
    assert sent["payload"]["tools"][0]["function"]["name"] == "web_search"
    # the response's tool call came back parsed, not dropped
    assert comp.stop_reason == "tool_use"
    assert len(comp.tool_calls) == 1
    tc = comp.tool_calls[0]
    assert tc.name == "web_search" and tc.args == {"query": "med spa Dubai", "num": 3}


def test_ollama_provider_with_no_tool_call_in_response_still_finalizes(monkeypatch):
    """A plain text reply (no tool_calls key at all) must not crash the parser and must finalize."""
    import httpx

    def fake_post(url, json, timeout, headers):
        body = {"choices": [{"message": {"content": "Here's the answer."}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    p = llm.OllamaProvider(model="qwen2.5:7b", base_url="http://localhost:11434")
    comp = p.complete(system="", messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=50)
    assert comp.stop_reason == "end" and comp.tool_calls == [] and comp.text == "Here's the answer."


def test_ollama_provider_tolerates_malformed_tool_call_arguments(monkeypatch):
    """A model that emits invalid JSON in tool_calls[].function.arguments must get an empty-args
    ToolCall, not crash the whole run."""
    import httpx

    def fake_post(url, json, timeout, headers):
        body = {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "web_search", "arguments": "{not json"}}]}}],
                "usage": {}}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    p = llm.OllamaProvider(model="qwen2.5:7b", base_url="http://localhost:11434")
    comp = p.complete(system="", messages=[{"role": "user", "content": "hi"}],
                      tools=[{"name": "web_search", "input_schema": {}}], max_tokens=50)
    assert comp.tool_calls[0].args == {}  # didn't crash, just came back empty


def test_plan_still_works_after_the_chat_signature_change():
    """plan() calls the same _chat() that complete() does — confirm the extra tool_calls return
    value didn't break the unrelated planning path (EchoProvider still exercised elsewhere; this
    checks the OllamaProvider retry loop's unpacking directly)."""
    import json as jsonlib

    import httpx

    def fake_post(url, json, timeout, headers):
        body = {"choices": [{"message": {"content": jsonlib.dumps({"tasks": [
            {"temp_id": "t1", "goal": "g", "department": "Sales", "est_effort_hours": 1, "depends_on": []}]})}}],
                "usage": {}}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    real_post = httpx.post
    try:
        httpx.post = fake_post
        p = llm.OllamaProvider(model="qwen2.5:7b", base_url="http://localhost:11434")
        result = p.plan(goal="ship a thing", departments=["Sales"], max_tokens=200)
        assert result.tasks[0]["goal"] == "g"
    finally:
        httpx.post = real_post


# ---------- web_search tool wiring ----------

def test_web_search_is_registered_and_granted_to_new_orgs(db):
    org_id = _org(db)
    reg = db.scalars(select(ToolRegistration).where(
        ToolRegistration.org_id == org_id, ToolRegistration.name == "web_search")).first()
    assert reg is not None
    # every department agent has its OWN profile now (not a shared "Worker" one) — check one of them
    worker = db.scalars(select(AgentProfile).where(
        AgentProfile.org_id == org_id, AgentProfile.name == "Sam Sales Agent")).first()
    assert "web_search" in worker.tool_grants


def test_web_search_tool_calls_real_serper_and_returns_results(db, monkeypatch):
    org_id = _org(db)
    monkeypatch.setattr("app.services.research.serper_search",
                        lambda query, num=6: [{"title": "T", "link": "L", "snippet": "S"}])
    out = tools.execute(db, org_id, ["web_search"], "web_search", {"query": "med spa Dubai"})
    assert out == {"results": [{"title": "T", "link": "L", "snippet": "S"}]}


def test_web_search_tool_rejects_empty_query(db):
    org_id = _org(db)
    out = tools.execute(db, org_id, ["web_search"], "web_search", {"query": "  "})
    assert out["error"] == "query is required" and out["results"] == []


def test_web_search_num_is_capped_at_ten(db, monkeypatch):
    org_id = _org(db)
    seen = {}
    monkeypatch.setattr("app.services.research.serper_search",
                        lambda query, num=6: seen.setdefault("num", num) or [])
    tools.execute(db, org_id, ["web_search"], "web_search", {"query": "x", "num": 500})
    assert seen["num"] == 10


# ---------- startup backfill for pre-existing orgs ----------

def test_backfill_registers_and_grants_web_search_for_an_org_missing_it(db):
    """Simulates an org created BEFORE web_search existed: strip the registration + grant that
    create_org would now add, then confirm the idempotent backfill restores both."""
    from app.main import backfill_web_search

    org_id = _org(db)
    reg = db.scalars(select(ToolRegistration).where(
        ToolRegistration.org_id == org_id, ToolRegistration.name == "web_search")).first()
    db.delete(reg)
    # every department agent has its own profile now — strip the grant from all of them to simulate
    # a pre-web_search org (they all shared the same grant shape before this feature existed too)
    workers = list(db.scalars(select(AgentProfile).where(
        AgentProfile.org_id == org_id, AgentProfile.name == "Sam Sales Agent")))
    for w in workers:
        w.tool_grants = ["echo", "get_time"]  # pre-web_search shape
    db.commit()

    backfill_web_search(db)

    assert db.scalars(select(ToolRegistration).where(
        ToolRegistration.org_id == org_id, ToolRegistration.name == "web_search")).first() is not None
    for w in workers:
        db.refresh(w)
        assert "web_search" in w.tool_grants


def test_backfill_is_idempotent_and_doesnt_duplicate_the_registration(db):
    from app.main import backfill_web_search

    org_id = _org(db)
    backfill_web_search(db)
    backfill_web_search(db)  # run twice — must not create a second row
    regs = list(db.scalars(select(ToolRegistration).where(
        ToolRegistration.org_id == org_id, ToolRegistration.name == "web_search")))
    assert len(regs) == 1


# ---------- the pointless "echo" tool must never reach a real model ----------

def test_a_real_provider_never_sees_the_echo_tool(db, monkeypatch):
    """"echo" only exists so EchoProvider's deterministic finalize step has something to round-trip
    through — it returns whatever text it's given, which is meaningless for a real model and has
    caused real agents to route their whole answer through it instead of just answering. A real
    provider must never even see it as an available tool."""
    from app.models import Actor
    from app.services import runs

    org_id = _org(db)
    sam = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.name == "Sam Sales Agent")).first()
    prof = db.get(AgentProfile, sam.agent_profile_id)
    prof.provider, prof.model = "mistral", "mistral-small-latest"
    db.commit()

    seen_tools = {}

    class _FakeProvider:
        def complete(self, *, system, messages, tools, max_tokens):
            seen_tools["names"] = [t["name"] for t in tools]
            from app.services.llm import Completion
            return Completion(text="done", tool_calls=[], input_tokens=1, output_tokens=1)

    monkeypatch.setattr(runs, "build_provider", lambda *a, **k: _FakeProvider())
    runs.execute(db, runs.create_run(db, org_id, sam, "do the thing"))

    assert "echo" not in seen_tools["names"]
    assert "web_search" in seen_tools["names"]  # real tools still offered


def test_echo_provider_still_sees_the_echo_tool_for_its_own_deterministic_path(db):
    """The opposite: Echo mode (the default/demo provider) must keep seeing "echo" — its finalize
    step depends on a tool that returns the text it was given verbatim."""
    from app.models import Actor
    from app.services import runs

    org_id = _org(db)
    sam = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.name == "Sam Sales Agent")).first()
    run = runs.execute(db, runs.create_run(db, org_id, sam, "hello"))
    assert run.status == "succeeded"
    assert "hello" in run.result["text"]
