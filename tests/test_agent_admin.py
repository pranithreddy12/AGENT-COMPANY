"""Agents tab: real per-agent edit + a real bucketed work history (not flat goal strings)."""
from sqlalchemy import select

from app.models import Actor, AgentProfile, Task
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import planning


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def _sam(db, org_id):
    return db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.name == "Sam Sales Agent")).first()


def test_list_agents_exposes_editable_fields(db):
    from app.routers.intelligence import list_agents

    org_id = _org(db)
    out = {a["name"]: a for a in list_agents(db, _p(org_id))}
    sam = out["Sam Sales Agent"]
    assert sam["system_prompt"] and sam["max_turns"] and sam["max_tokens"] is not None
    assert sam["cost_ceiling_usd"] is not None and sam["profile_id"]


def test_history_buckets_completed_tasks_with_a_real_timestamp(db):
    from app.routers.intelligence import list_agents

    org_id = _org(db)
    project, _ = planning.draft_project(db, org_id, "Deliver engagement")
    db.commit()
    planning.approve_project(db, project)
    planning.execute_project(db, project)

    out = {a["name"]: a for a in list_agents(db, _p(org_id))}
    completed_somewhere = [a for a in out.values() if a["history"]["completed"]]
    assert completed_somewhere
    entry = completed_somewhere[0]["history"]["completed"][0]
    assert entry["status"] == "done" and entry["project_goal"] == "Deliver engagement"
    assert entry["when"] is not None  # a real artifact creation timestamp, not fabricated


def test_history_buckets_scheduled_tasks_separately_from_in_progress(db):
    """Not-yet-run tasks (proposed/scheduled) must land in 'scheduled', never silently merged with
    'in_progress' — that distinction is the whole point of the fix."""
    from app.routers.intelligence import list_agents

    org_id = _org(db)
    project, tasks = planning.draft_project(db, org_id, "Deliver engagement")  # planning only, never executed
    db.commit()

    out = list_agents(db, _p(org_id))
    any_scheduled = any(a["history"]["scheduled"] for a in out)
    any_in_progress = any(a["history"]["in_progress"] for a in out)
    any_completed = any(a["history"]["completed"] for a in out)
    assert any_scheduled  # freshly drafted tasks are proposed/scheduled, not yet run
    assert not any_in_progress and not any_completed


def test_update_agent_system_prompt_and_autonomy(db):
    from app.routers.intelligence import update_agent
    from app.schemas import AgentProfileUpdate

    org_id = _org(db)
    sam = _sam(db, org_id)
    result = update_agent(sam.id, AgentProfileUpdate(system_prompt="Sell only to enterprise accounts.",
                                                      autonomy_default="L2"), db, _p(org_id, role="ceo"))
    assert result["system_prompt"] == "Sell only to enterprise accounts." and result["autonomy"] == "L2"
    prof = db.get(AgentProfile, sam.agent_profile_id)
    assert prof.system_prompt == "Sell only to enterprise accounts." and prof.autonomy_default == "L2"


def test_update_agent_rejects_invalid_autonomy(db):
    from fastapi import HTTPException

    from app.routers.intelligence import update_agent
    from app.schemas import AgentProfileUpdate

    org_id = _org(db)
    sam = _sam(db, org_id)
    try:
        update_agent(sam.id, AgentProfileUpdate(autonomy_default="L99"), db, _p(org_id, role="ceo"))
        assert False, "should have rejected an invalid autonomy level"
    except HTTPException as e:
        assert e.status_code == 422


def test_update_agent_blank_fields_leave_the_rest_untouched(db):
    from app.routers.intelligence import update_agent
    from app.schemas import AgentProfileUpdate

    org_id = _org(db)
    sam = _sam(db, org_id)
    before = db.get(AgentProfile, sam.agent_profile_id).system_prompt
    update_agent(sam.id, AgentProfileUpdate(autonomy_default="L2"), db, _p(org_id, role="ceo"))  # prompt omitted
    prof = db.get(AgentProfile, sam.agent_profile_id)
    assert prof.system_prompt == before and prof.autonomy_default == "L2"


def test_update_agent_requires_ceo_or_dept_head(db):
    """The actual guard on PATCH /agents/{id} is the require_role("ceo","dept_head") FastAPI
    dependency — calling update_agent directly bypasses dependency injection entirely, so this
    exercises the SAME dependency function the router declares, not a re-implementation of it."""
    from fastapi import HTTPException

    from app.auth import require_role

    org_id = _org(db)
    try:
        require_role("ceo", "dept_head")(_p(org_id, role="member"))
        assert False, "a plain member must not be able to edit an agent's config"
    except HTTPException as e:
        assert e.status_code == 403


def _p(org_id, role="ceo"):
    from app.auth import Principal
    return Principal(user_id="u1", org_id=org_id, role=role)
