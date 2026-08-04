"""No-key hardening for the 'prove the intelligence' work: plan validation, critic parsing,
SOP-as-system-prompt, and the eval harness (all on Echo)."""
import pytest

from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import evals, llm, review


# --- item 4: Anthropic plan validation (pure, no key) ---

def test_validate_plan_accepts_good():
    data = {"tasks": [
        {"temp_id": "t1", "goal": "a", "department": "Dev", "est_effort_hours": 2, "depends_on": []},
        {"temp_id": "t2", "goal": "b", "department": "Dev", "est_effort_hours": 1, "depends_on": ["t1"]},
    ]}
    assert len(llm.validate_plan(data)) == 2


@pytest.mark.parametrize("bad", [
    {},
    {"tasks": []},
    {"tasks": [{"temp_id": "t1", "goal": "a", "department": "D", "est_effort_hours": 1}]},  # no depends_on
    {"tasks": [{"temp_id": "t1", "goal": "a", "department": "D", "est_effort_hours": "x", "depends_on": []}]},
    {"tasks": [{"temp_id": "t1", "goal": "a", "department": "D", "est_effort_hours": 1, "depends_on": ["ghost"]}]},
])
def test_validate_plan_fails_closed(bad):
    with pytest.raises(llm.PlanParseError):
        llm.validate_plan(bad)


# --- item 3: critic verdict parsing (pure, no key) ---

def test_mistral_provider_wiring():
    from app.services import cost
    from app.services.llm import MistralProvider
    p = MistralProvider(api_key="k", model="mistral-small-latest", base_url="https://api.mistral.ai")
    assert p.api_key == "k" and p.base_url == "https://api.mistral.ai"
    # cost rate registered so runs don't fail-closed on an unknown model
    assert cost.compute("mistral-small-latest", 1000, 1000) == round((1000 * 0.2 + 1000 * 0.6) / 1e6, 6)


def test_parse_critic_verdict():
    assert review.parse_critic_verdict('{"passed": true, "reasons": []}').passed is True
    v = review.parse_critic_verdict('{"passed": false, "reasons": ["add pricing"]}')
    assert v.passed is False and v.reasons == ["add pricing"]
    # unparseable -> fail closed (revise), never a silent pass
    assert review.parse_critic_verdict("not json").passed is False


# --- items 1 + 5: eval harness on Echo (exercises the real code paths) ---

def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def test_eval_harness_runs_on_echo(db):
    org_id = _org(db)
    out = evals.run_all(db, org_id)
    assert out["total"] == 3
    by_name = {r["name"]: r for r in out["results"]}
    assert by_name["lead_decomposition"]["passed"]
    assert by_name["sop_behavior"]["passed"]      # SOP-via-system-prompt changes output
    assert by_name["critic"]["passed"]


def test_sop_reaches_agent_via_system_prompt(db):
    # the mechanism is real: a RULE in the system prompt shows in echo output, absent otherwise
    from app.services.llm import EchoProvider
    p = EchoProvider()
    with_rule = p.complete(system="You are X.\nRULE: sign every note", messages=[{"role": "tool", "content": "x"}],
                           tools=[], max_tokens=64)
    without = p.complete(system="You are X.", messages=[{"role": "tool", "content": "x"}], tools=[], max_tokens=64)
    assert "sign every note" in with_rule.text and "SOP applied" not in without.text
