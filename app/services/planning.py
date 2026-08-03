"""The Lead + project lifecycle. The Lead plans and routes; it never does the work.

draft -> approve(schedule) -> execute(artifacts). Every Lead planning pass is an audited
AgentRun with cost. Task execution reuses the Phase 0 worker executor.
"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Actor, AgentProfile, AgentRun, Artifact, Department, Playbook, Project, Task
from app.services import cost, communication, events, playbooks, review, runs, scheduling
from app.services.llm import build_provider

MAX_REVISE_CYCLES = 2  # Critic revise loop cap -> escalate to human. Bounds the loop.


class PlanError(Exception):
    pass


def _lead(db: Session, org_id: str) -> Actor:
    lead = db.scalars(
        select(Actor).where(Actor.org_id == org_id, Actor.type == "agent", Actor.role == "lead")
    ).first()
    if lead is None:
        raise PlanError("no Lead agent in org")
    return lead


def _nodes(tasks: list[Task]) -> list[dict]:
    return [{"id": t.id, "effort": t.est_effort_hours, "deps": list(t.depends_on)} for t in tasks]


def _tasks(db: Session, project: Project) -> list[Task]:
    return list(db.scalars(select(Task).where(Task.project_id == project.id)))


def draft_project(db: Session, org_id: str, goal: str, account_id: str | None = None) -> tuple[Project, list[Task]]:
    project = Project(org_id=org_id, goal=goal, account_id=account_id, status="planning", health="unknown")
    db.add(project)
    db.flush()

    lead = _lead(db, org_id)
    profile = db.get(AgentProfile, lead.agent_profile_id)
    run = AgentRun(org_id=org_id, actor_id=lead.id, trigger=goal, status="running")
    db.add(run)
    db.flush()
    events.append(db, org_id=org_id, trace_id=run.trace_id, run_id=run.id, actor_id=lead.id,
                  action="run.started", after={"trigger": goal})

    depts = {d.name: d for d in db.scalars(select(Department).where(Department.org_id == org_id))}
    if not depts:
        raise PlanError("no departments to route to")
    default_dept = depts.get("Development") or next(iter(depts.values()))

    provider = build_provider(profile.provider, profile.model, settings.anthropic_api_key)
    try:
        pr = provider.plan(goal=goal, departments=list(depts), max_tokens=profile.max_tokens)
    except Exception as e:  # fail closed
        runs._finish(db, run, "failed", error=f"plan: {e}")
        raise PlanError(str(e))

    step_cost = cost.compute(profile.model, pr.input_tokens, pr.output_tokens)
    run.cost_usd = step_cost
    events.append(db, org_id=org_id, trace_id=run.trace_id, run_id=run.id, actor_id=lead.id,
                  action="model.call", target=profile.model, cost_usd=step_cost,
                  after={"n_tasks": len(pr.tasks)})

    # materialize: temp_id -> Task, wire deps, round-robin assign to the routed department's agents
    id_map: dict[str, Task] = {}
    dev_agents: dict[str, list[Actor]] = {}
    for spec in pr.tasks:
        dept = depts.get(spec.get("department"), default_dept)
        t = Task(
            org_id=org_id, project_id=project.id, goal=spec["goal"],
            acceptance_criteria=spec.get("acceptance_criteria", ""),
            department_id=dept.id, est_effort_hours=float(spec["est_effort_hours"]),
            depends_on=[], status="proposed",
        )
        db.add(t)
        db.flush()
        id_map[spec["temp_id"]] = t

    for spec in pr.tasks:
        t = id_map[spec["temp_id"]]
        t.depends_on = [id_map[d].id for d in spec.get("depends_on", []) if d in id_map]
        # assign a member agent of the routed department (round-robin)
        dept_id = t.department_id
        if dept_id not in dev_agents:
            dev_agents[dept_id] = list(db.scalars(
                select(Actor).where(Actor.org_id == org_id, Actor.department_id == dept_id,
                                    Actor.type == "agent", Actor.role == "member")
            ))
        pool = dev_agents[dept_id]
        if pool:
            t.assignee_actor_id = pool[list(id_map).index(spec["temp_id"]) % len(pool)].id

    tasks = list(id_map.values())
    try:
        scheduling.topo_order(_nodes(tasks))  # validate DAG before we accept the plan
    except scheduling.CycleError as e:
        runs._finish(db, run, "failed", error=f"invalid plan: {e}")
        raise PlanError(str(e))

    runs._finish(db, run, "succeeded", result={"task_ids": [t.id for t in tasks]})
    return project, tasks


def _write_schedule(db: Session, project: Project) -> dict:
    tasks = _tasks(db, project)
    slots, finish, crit = scheduling.schedule(_nodes(tasks))
    for t in tasks:
        s = slots[t.id]
        t.est_start_h, t.est_finish_h, t.slack_h, t.is_critical = s.est_start, s.est_finish, s.slack, s.critical
        t.due_at = project.start_at + timedelta(hours=s.est_finish)
    project.due_at = project.start_at + timedelta(hours=finish)
    db.flush()
    return {"project_finish_h": finish, "critical_path": crit}


def approve_project(db: Session, project: Project) -> dict:
    summary = _write_schedule(db, project)
    for t in _tasks(db, project):
        if t.status == "proposed":
            t.status = "scheduled"
    project.status = "active"
    project.health = "on_track"
    db.commit()
    return summary


def _critic_actor(db: Session, org_id: str) -> Actor | None:
    return db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.role == "critic")).first()


def _review(db: Session, org_id: str, critic: Actor | None, content: str, criteria: str, playbook: str):
    """Use the real LLM Critic when the Critic agent is configured for a real provider; otherwise
    the deterministic critic. Same Verdict interface either way."""
    prof = db.get(AgentProfile, critic.agent_profile_id) if critic and critic.agent_profile_id else None
    if prof and prof.provider != "echo":
        provider = build_provider(prof.provider, prof.model, settings.anthropic_api_key)
        return review.llm_critic_review(provider, content, criteria, playbook)
    return review.critic_review(content, criteria, playbook)


def judge(db: Session, org_id: str, content: str, criteria: str, playbook: str = ""):
    """Public: review arbitrary content through the org's Critic (real or deterministic)."""
    return _review(db, org_id, _critic_actor(db, org_id), content, criteria, playbook)


def _playbook_md(db: Session, org_id: str, dept_id: str | None) -> str:
    if not dept_id:
        return ""
    pb = playbooks.active(db, org_id, dept_id)
    return pb.markdown if pb else ""


def _run_and_review(db: Session, project: Project, task: Task, critic: Actor | None) -> Artifact:
    """Produce an artifact, then run the Critic. Re-run on `revise`, capped at MAX_REVISE_CYCLES,
    then escalate to a human. This cap is the bounded-loop guarantee.
    """
    actor = db.get(Actor, task.assignee_actor_id)
    active_pb = playbooks.active(db, project.org_id, task.department_id) if task.department_id else None
    playbook = active_pb.markdown if active_pb else ""
    art = Artifact(org_id=project.org_id, task_id=task.id, type="doc", version=0,
                   produced_by_actor_id=actor.id, status="produced",
                   playbook_version=active_pb.version if active_pb else None)
    db.add(art)

    feedback = ""
    for _ in range(MAX_REVISE_CYCLES + 1):  # initial attempt + N revises
        # the active Playbook goes into the agent's system context (real in-context SOP loading)
        run = runs.execute(db, runs.create_run(db, project.org_id, actor, task.goal + feedback),
                           extra_system=playbook)
        if run.status != "succeeded":
            # fail closed: a failed run never becomes a passing artifact — escalate to a human
            art.content, art.version = (run.error or "run failed"), art.version + 1
            art.needs_human, art.critic_reasons = True, [f"run failed: {run.error}"]
            return art
        content = (run.result or {}).get("text", "")
        art.content, art.version = content, art.version + 1
        verdict = _review(db, project.org_id, critic, content, task.acceptance_criteria, playbook)
        art.reviewed_by_actor_id = critic.id if critic else None
        if verdict.passed:
            art.status, art.critic_reasons = "reviewed", []
            return art
        art.critic_reasons = verdict.reasons
        feedback = "\nRevise: " + "; ".join(verdict.reasons)

    # cap exhausted -> escalate to a human, don't loop forever
    art.needs_human = True
    thread = communication.create_thread(
        db, project.org_id, "escalation", subject=f"Critic could not pass: {task.goal}", project_id=project.id
    )
    communication.post_message(db, thread, critic.id if critic else None,
                               "Revise cap reached; escalating to a human. Reasons: " + "; ".join(art.critic_reasons))
    return art


def rerun_task(db: Session, project: Project, task: Task) -> Artifact:
    """Re-execute a single task (e.g. after a Playbook amendment). Produces a fresh Artifact."""
    critic = _critic_actor(db, project.org_id)
    art = _run_and_review(db, project, task, critic)
    task.status = "done" if not art.needs_human else "blocked"
    db.commit()
    return art


def execute_project(db: Session, project: Project) -> list[Artifact]:
    tasks = {t.id: t for t in _tasks(db, project)}
    order = scheduling.topo_order(_nodes(list(tasks.values())))
    depts = {d.id: d for d in db.scalars(select(Department).where(Department.org_id == project.org_id))}
    critic = _critic_actor(db, project.org_id)
    artifacts_by_task: dict[str, Artifact] = {}

    for tid in order:
        t = tasks[tid]
        if t.assignee_actor_id is None:
            continue

        # structured handoff for every cross-department dependency edge
        for dep_id in t.depends_on:
            dep = tasks.get(dep_id)
            if dep and dep.department_id and t.department_id and dep.department_id != t.department_id:
                dep_art = artifacts_by_task.get(dep_id)
                communication.make_handoff(
                    db, org_id=project.org_id, project_id=project.id,
                    from_dept=dep.department_id, to_dept=t.department_id,
                    context=f"{depts[dep.department_id].name} → {depts[t.department_id].name}: {t.goal}",
                    evidence=[dep_art.content] if dep_art else [],
                    sender_actor_id=dep.assignee_actor_id,
                )

        art = _run_and_review(db, project, t, critic)
        artifacts_by_task[t.id] = art

        # Legal veto: a Legal task blocks any already-produced artifact with prohibited content.
        if depts.get(t.department_id) and depts[t.department_id].name == "Legal":
            for other in artifacts_by_task.values():
                v = review.legal_review(other.content)
                if not v.passed:
                    other.blocked, other.block_reason = True, "; ".join(v.reasons)

        t.status = "done" if not art.needs_human else "blocked"

    project.status = "done" if all(x.status == "done" for x in tasks.values()) else "active"
    project.health = "on_track" if project.status == "done" else project.health
    db.commit()
    return list(artifacts_by_task.values())


def slip_task(db: Session, project: Project, task: Task, added_hours: float) -> dict:
    old_finish = project.due_at
    task.est_effort_hours = round(task.est_effort_hours + added_hours, 6)
    db.flush()
    summary = _write_schedule(db, project)  # rebuilds nodes from the bumped effort
    if old_finish is not None and project.due_at > old_finish:
        project.health = "slipping"
    db.commit()
    summary["health"] = project.health
    return summary
