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


def test_every_department_agent_has_its_own_profile_not_a_shared_one(db):
    """The real bug this fixes: all 7 department agents used to point at ONE shared AgentProfile
    row, so editing "Sam" via PATCH /agents/{id} silently rewrote Piper, Mia, Devin, Dana, Lena, and
    Cleo too. Each must have a distinct profile id and a distinct system_prompt now."""
    org_id = _org(db)
    names = ["Piper Planning Agent", "Sam Sales Agent", "Mia Marketing Agent", "Devin Dev Agent",
             "Dana Dev Agent", "Lena Legal Agent", "Cleo Client Agent"]
    profiles = []
    for name in names:
        actor = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.name == name)).first()
        profiles.append(db.get(AgentProfile, actor.agent_profile_id))
    assert len({p.id for p in profiles}) == len(names)  # every agent has its OWN profile row
    assert len({p.system_prompt for p in profiles}) == len(names)  # every one is a distinct voice


def test_editing_one_agent_never_changes_another(db):
    """The exact regression: PATCH one agent's system_prompt/autonomy and confirm every other
    agent's profile is completely untouched."""
    from app.routers.intelligence import update_agent
    from app.schemas import AgentProfileUpdate

    org_id = _org(db)
    sam = _sam(db, org_id)
    mia = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.name == "Mia Marketing Agent")).first()
    mia_prof_before = db.get(AgentProfile, mia.agent_profile_id)
    mia_prompt_before, mia_autonomy_before = mia_prof_before.system_prompt, mia_prof_before.autonomy_default
    assert mia_autonomy_before != "L2"  # sanity: distinguishable from what we're about to set on Sam

    update_agent(sam.id, AgentProfileUpdate(system_prompt="Sell only to enterprise accounts.",
                                            autonomy_default="L2"), db, _p(org_id, role="ceo"))

    mia_prof = db.get(AgentProfile, mia.agent_profile_id)
    assert mia_prof.system_prompt == mia_prompt_before  # completely untouched
    assert mia_prof.autonomy_default == mia_autonomy_before  # not silently bumped to L2 too


def test_backfill_splits_a_legacy_shared_profile_into_real_personas(db):
    """Simulates a pre-persona-split org: all persona actors pointed at one shared 'Worker' profile.
    Backfill must split each into its own profile with the real voice, without duplicating work."""
    from app.main import backfill_agent_personas
    from app.routers.orgs import PERSONAS

    org_id = _org(db)
    shared = AgentProfile(org_id=org_id, name="Worker", system_prompt="generic", provider="echo",
                          model="echo-1", max_turns=4, tool_grants=["echo"])
    db.add(shared)
    db.flush()
    actors = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.name.in_(PERSONAS.keys()))).all()
    for a in actors:
        a.agent_profile_id = shared.id
    db.commit()

    backfill_agent_personas(db)
    db.commit()

    profile_ids = set()
    for a in actors:
        db.refresh(a)
        prof = db.get(AgentProfile, a.agent_profile_id)
        assert prof.name == a.name
        assert prof.system_prompt.startswith(PERSONAS[a.name][1][:30])
        profile_ids.add(prof.id)
    assert len(profile_ids) == len(actors)  # each got its own row, none still shared

    # idempotent: running again doesn't create yet more profiles
    backfill_agent_personas(db)
    db.commit()
    for a in actors:
        db.refresh(a)
    assert {db.get(AgentProfile, a.agent_profile_id).id for a in actors} == profile_ids
