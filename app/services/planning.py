"""The Lead + project lifecycle. The Lead plans and routes; it never does the work.

draft -> approve(schedule) -> execute(artifacts). Every Lead planning pass is an audited
AgentRun with cost. Task execution reuses the Phase 0 worker executor.
"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Actor,
    AgentProfile,
    AgentRun,
    Artifact,
    Department,
    MemoryRecord,
    Playbook,
    Project,
    Task,
    Thread,
)
from app.services import (
    cost,
    communication,
    events,
    playbooks,
    research,
    review,
    runs,
    scheduling,
)
from app.services.llm import build_provider, resolve_api_key

MAX_REVISE_CYCLES = 2  # Critic revise loop cap -> escalate to human. Bounds the loop.


class PlanError(Exception):
    pass


def _lead(db: Session, org_id: str) -> Actor:
    lead = db.scalars(
        select(Actor).where(
            Actor.org_id == org_id, Actor.type == "agent", Actor.role == "lead"
        )
    ).first()
    if lead is None:
        raise PlanError("no Lead agent in org")
    return lead


def _nodes(tasks: list[Task]) -> list[dict]:
    return [
        {"id": t.id, "effort": t.est_effort_hours, "deps": list(t.depends_on)}
        for t in tasks
    ]


def _tasks(db: Session, project: Project) -> list[Task]:
    return list(db.scalars(select(Task).where(Task.project_id == project.id)))


def draft_project(
    db: Session, org_id: str, goal: str, account_id: str | None = None
) -> tuple[Project, list[Task]]:
    project = Project(
        org_id=org_id,
        goal=goal,
        account_id=account_id,
        status="planning",
        health="unknown",
    )
    db.add(project)
    db.flush()

    lead = _lead(db, org_id)
    profile = db.get(AgentProfile, lead.agent_profile_id)
    run = AgentRun(org_id=org_id, actor_id=lead.id, trigger=goal, status="running")
    db.add(run)
    db.flush()
    events.append(
        db,
        org_id=org_id,
        trace_id=run.trace_id,
        run_id=run.id,
        actor_id=lead.id,
        action="run.started",
        after={"trigger": goal},
    )

    depts = {
        d.name: d
        for d in db.scalars(select(Department).where(Department.org_id == org_id))
    }
    if not depts:
        raise PlanError("no departments to route to")
    default_dept = depts.get("Development") or next(iter(depts.values()))

    provider = build_provider(
        profile.provider, profile.model, resolve_api_key(db, org_id, profile.provider)
    )
    try:
        pr = provider.plan(
            goal=goal, departments=list(depts), max_tokens=max(profile.max_tokens, 4096)
        )
    except Exception as e:  # fail closed
        runs._finish(db, run, "failed", error=f"plan: {e}")
        raise PlanError(str(e))

    step_cost = cost.compute(profile.model, pr.input_tokens, pr.output_tokens)
    run.cost_usd = step_cost
    events.append(
        db,
        org_id=org_id,
        trace_id=run.trace_id,
        run_id=run.id,
        actor_id=lead.id,
        action="model.call",
        target=profile.model,
        cost_usd=step_cost,
        after={"n_tasks": len(pr.tasks)},
    )

    # materialize: temp_id -> Task, wire deps, round-robin assign to the routed department's agents
    id_map: dict[str, Task] = {}
    dev_agents: dict[str, list[Actor]] = {}
    for spec in pr.tasks:
        dept = depts.get(spec.get("department"), default_dept)
        t = Task(
            org_id=org_id,
            project_id=project.id,
            goal=spec["goal"],
            acceptance_criteria=spec.get("acceptance_criteria", ""),
            department_id=dept.id,
            est_effort_hours=float(spec["est_effort_hours"]),
            depends_on=[],
            status="proposed",
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
            dev_agents[dept_id] = list(
                db.scalars(
                    select(Actor).where(
                        Actor.org_id == org_id,
                        Actor.department_id == dept_id,
                        Actor.type == "agent",
                        Actor.role == "member",
                    )
                )
            )
        pool = dev_agents[dept_id]
        if pool:
            t.assignee_actor_id = pool[
                list(id_map).index(spec["temp_id"]) % len(pool)
            ].id

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
        t.est_start_h, t.est_finish_h, t.slack_h, t.is_critical = (
            s.est_start,
            s.est_finish,
            s.slack,
            s.critical,
        )
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
    return db.scalars(
        select(Actor).where(Actor.org_id == org_id, Actor.role == "critic")
    ).first()


def _review(
    db: Session,
    org_id: str,
    critic: Actor | None,
    content: str,
    criteria: str,
    playbook: str,
):
    """Use the real LLM Critic when the Critic agent is configured for a real provider; otherwise
    the deterministic critic. Same Verdict interface either way."""
    prof = (
        db.get(AgentProfile, critic.agent_profile_id)
        if critic and critic.agent_profile_id
        else None
    )
    if prof and prof.provider != "echo":
        provider = build_provider(
            prof.provider, prof.model, resolve_api_key(db, org_id, prof.provider)
        )
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


_CONTEXT_ARTIFACT_CAP = (
    2400  # chars of each upstream deliverable shown to downstream agents
)


def _clip(text: str, cap: int) -> str:
    """Clip to `cap` chars at a line boundary when possible, so an agent never reads half a sentence."""
    if len(text) <= cap:
        return text
    cut = text.rfind("\n", 0, cap)
    return text[: cut if cut > cap // 2 else cap] + "\n…[continued]"


def _brief(task: Task, dept_name: str | None, context: str) -> str:
    """What the producing agent actually reads. Carries the acceptance criteria — the Critic judges
    against them, so the agent must see the same bar — plus concrete-output rules and team context."""
    b = f"Your department: {dept_name or 'General'}.\nYour task: {task.goal}"
    if (task.acceptance_criteria or "").strip():
        b += (
            "\nAcceptance criteria — QA will hold your deliverable to exactly these, so meet every "
            f"one:\n{task.acceptance_criteria.strip()}"
        )
    b += (
        "\nProduce ONE concrete, ready-to-use deliverable that satisfies every criterion: specific "
        "steps, real numbers and targets, named tactics and decisions. Never placeholders like "
        "[Company Name], [Insert X], [Date] or 'Example'; never generic advice; do not restate the "
        "task or narrate — just deliver."
    )
    if context:
        b = (
            "Context from your team — build directly on this, be specific to it, and stay consistent "
            "with what they produced:\n" + context + "\n\n---\n" + b
        )
    return b


def _run_and_review(
    db: Session, project: Project, task: Task, critic: Actor | None, context: str = ""
) -> Artifact:
    """Produce an artifact, then run the Critic. Re-run on `revise`, capped at MAX_REVISE_CYCLES,
    then escalate to a human. This cap is the bounded-loop guarantee.

    `context` is the shared team context (upstream deliverables + project memory) — the agent reads
    it before working so its output builds on the team's, not a blank slate.
    """
    actor = db.get(Actor, task.assignee_actor_id)
    dept = db.get(Department, task.department_id) if task.department_id else None
    active_pb = (
        playbooks.active(db, project.org_id, task.department_id)
        if task.department_id
        else None
    )
    playbook = active_pb.markdown if active_pb else ""
    art = Artifact(
        org_id=project.org_id,
        task_id=task.id,
        type="doc",
        version=0,
        produced_by_actor_id=actor.id,
        status="produced",
        playbook_version=active_pb.version if active_pb else None,
    )
    db.add(art)

    brief = _brief(task, dept.name if dept else None, context)

    feedback = ""
    for _ in range(MAX_REVISE_CYCLES + 1):  # initial attempt + N revises
        # the active Playbook goes into the agent's system context (real in-context SOP loading)
        run = runs.execute(
            db,
            runs.create_run(db, project.org_id, actor, brief + feedback),
            extra_system=playbook,
        )
        if run.status != "succeeded":
            # fail closed: a failed run never becomes a passing artifact — escalate to a human
            art.content, art.version = (run.error or "run failed"), art.version + 1
            art.needs_human, art.critic_reasons = True, [f"run failed: {run.error}"]
            return art
        content = (run.result or {}).get("text", "")
        art.content, art.version = content, art.version + 1
        try:
            verdict = _review(
                db, project.org_id, critic, content, task.acceptance_criteria, playbook
            )
        except (
            Exception
        ) as e:  # a Critic API failure escalates to a human — never crashes execution
            art.needs_human, art.critic_reasons = True, [f"critic error: {e}"]
            return art
        art.reviewed_by_actor_id = critic.id if critic else None
        if verdict.passed:
            art.status, art.critic_reasons = "reviewed", []
            return art
        art.critic_reasons = verdict.reasons
        # revision shows the agent its own rejected draft + every QA point, so it FIXES the draft
        # instead of regenerating from scratch and losing what was already good
        feedback = (
            "\n\nQA rejected your previous draft. Fix EVERY point below while keeping what "
            f"was good:\nQA reasons: {'; '.join(verdict.reasons)}"
            f"\n\n--- Your previous draft ---\n{content}\n--- end of previous draft ---"
        )

    # cap exhausted -> escalate to a human, don't loop forever
    art.needs_human = True
    thread = communication.create_thread(
        db,
        project.org_id,
        "escalation",
        subject=f"Critic could not pass: {task.goal}",
        project_id=project.id,
    )
    communication.post_message(
        db,
        thread,
        critic.id if critic else None,
        "Revise cap reached; escalating to a human. Reasons: "
        + "; ".join(art.critic_reasons),
    )
    return art


def _gather_context(
    db: Session,
    project: Project,
    task: Task,
    tasks: dict,
    artifacts_by_task: dict,
    depts: dict,
    include_memory: bool = True,
) -> str:
    """The shared context an agent reads before working: the real deliverables of its upstream
    dependencies (+ optionally the accumulated project memory). This is how the team builds on
    itself. `include_memory=False` for chat-assigned work, where one shared bucket's memory would
    mix unrelated requests into every answer."""
    parts = []
    for dep_id in task.depends_on:
        dep, art = tasks.get(dep_id), artifacts_by_task.get(dep_id)
        if dep and art and art.content:
            name = depts[dep.department_id].name if dep.department_id in depts else "?"
            parts.append(
                f"[{name}] {dep.goal}:\n{_clip(art.content.strip(), _CONTEXT_ARTIFACT_CAP)}"
            )
    if include_memory:
        mem = list(
            db.scalars(
                select(MemoryRecord)
                .where(
                    MemoryRecord.project_id == project.id,
                    MemoryRecord.scope == "project",
                )
                .order_by(MemoryRecord.created_at)
            )
        )
        if mem:
            parts.append(
                "Shared project knowledge so far:\n- "
                + "\n- ".join(m.content for m in mem[-8:])
            )
    return "\n\n".join(parts)


def _remember(
    db: Session, project: Project, task: Task, artifact: Artifact, depts: dict
) -> None:
    """Archivist: write what an agent produced into shared project memory for the rest of the team."""
    name = depts[task.department_id].name if task.department_id in depts else "?"
    summary = " ".join((artifact.content or "").split())[:220]
    db.add(
        MemoryRecord(
            org_id=project.org_id,
            scope="project",
            project_id=project.id,
            department_id=task.department_id,
            source_actor_id=artifact.produced_by_actor_id,
            content=f"{name} completed '{task.goal}': {summary}",
        )
    )
    db.flush()


def _status_thread(db: Session, project: Project) -> Thread:
    t = db.scalars(
        select(Thread).where(
            Thread.project_id == project.id, Thread.thread_type == "status"
        )
    ).first()
    if t is None:
        t = communication.create_thread(
            db,
            project.org_id,
            "status",
            subject=f"Team log: {project.goal[:40]}",
            project_id=project.id,
            message_budget=10000,
        )
    return t


def _post(db: Session, project: Project, actor_id: str | None, msg: str) -> None:
    communication.post_message(db, _status_thread(db, project), actor_id, msg)
    db.commit()  # commit immediately so the chat streams in live (readers see each message at once)


def _name(actors: dict, task: Task) -> str | None:
    a = actors.get(task.assignee_actor_id)
    return a.name if a and a.name else None


def _upstream_names(task: Task, tasks: dict, actors: dict) -> list[str]:
    return [n for d in task.depends_on if d in tasks and (n := _name(actors, tasks[d]))]


def _downstream_handoffs(task: Task, tasks: dict, actors: dict) -> list[str]:
    """'Dana on “Draft technical spec”' — who exactly picks up what next, so the handoff names real work."""
    out = []
    for o in tasks.values():
        if task.id in o.depends_on and (n := _name(actors, o)):
            label = " ".join(o.goal.split())
            out.append(f"{n} on “{label[:60]}{'…' if len(label) > 60 else ''}”")
    return sorted(out)


def _kickoff(
    db: Session, project: Project, tasks: dict, order: list, actors: dict
) -> None:
    lead = db.scalars(
        select(Actor).where(Actor.org_id == project.org_id, Actor.role == "lead")
    ).first()
    if not lead:
        return
    msg = f"{lead.name}: Plan set for “{project.goal}” — {len(tasks)} tasks across the team."
    moves = []
    for tid in order[:3]:  # opening sequence so everyone sees how the work flows
        t = tasks[tid]
        if n := _name(actors, t):
            goal = " ".join(t.goal.split())
            moves.append(f"{n} opens with “{goal[:60]}{'…' if len(goal) > 60 else ''}”")
    if moves:
        msg += " Opening moves: " + "; ".join(moves) + "."
    _post(db, project, lead.id, msg)


def _announce_start(
    db: Session, project: Project, task: Task, tasks: dict, actors: dict
) -> None:
    who = _name(actors, task) or "An agent"
    up = _upstream_names(task, tasks, actors)
    _post(
        db,
        project,
        task.assignee_actor_id,
        f"{who}: thanks {', '.join(up)} — picking up “{task.goal}” from here."
        if up
        else f"{who}: starting “{task.goal}”.",
    )


def _announce_done(
    db: Session, project: Project, task: Task, art: Artifact, tasks: dict, actors: dict
) -> None:
    who = _name(actors, task) or "An agent"
    gist = " ".join((art.content or "").split())[:100]
    handoffs = _downstream_handoffs(task, tasks, actors)
    _post(
        db,
        project,
        task.assignee_actor_id,
        f"{who}: done with “{task.goal}”. {gist}"
        + (
            f" Over to you: {', '.join(handoffs)}."
            if handoffs
            else " That wraps the project."
        ),
    )


def rerun_task(
    db: Session,
    project: Project,
    task: Task,
    extra_context: str = "",
    include_memory: bool = True,
) -> Artifact:
    """Re-execute a single task (e.g. after a Playbook amendment, or a chat-assigned request).
    Produces a fresh Artifact. `extra_context` is additional material the agent should read (e.g.
    the team-chat conversation around the request); `include_memory=False` skips shared project
    memory when it would mix unrelated work into this task."""
    critic = _critic_actor(db, project.org_id)
    tasks = {t.id: t for t in _tasks(db, project)}
    depts = {
        d.id: d
        for d in db.scalars(
            select(Department).where(Department.org_id == project.org_id)
        )
    }
    arts = (
        {
            a.task_id: a
            for a in db.scalars(
                select(Artifact).where(Artifact.task_id.in_(list(tasks)))
            )
        }
        if tasks
        else {}
    )
    context = _gather_context(
        db, project, task, tasks, arts, depts, include_memory=include_memory
    )
    if extra_context:
        context = f"{context}\n\n{extra_context}".strip()
    art = _run_and_review(db, project, task, critic, context=context)
    task.status = "done" if not art.needs_human else "blocked"
    db.commit()
    return art


def execute_project(db: Session, project: Project) -> list[Artifact]:
    tasks = {t.id: t for t in _tasks(db, project)}
    order = scheduling.topo_order(_nodes(list(tasks.values())))
    depts = {
        d.id: d
        for d in db.scalars(
            select(Department).where(Department.org_id == project.org_id)
        )
    }
    actors = {
        a.id: a for a in db.scalars(select(Actor).where(Actor.org_id == project.org_id))
    }
    critic = _critic_actor(db, project.org_id)
    artifacts_by_task: dict[str, Artifact] = {}
    _kickoff(db, project, tasks, order, actors)  # the Lead opens the team chat

    # Research agent goes first: web search on the goal -> sourced brief in shared memory for everyone
    rsummary = research.run_research(db, project)
    if rsummary:
        rex = research.rex(db, project.org_id)
        _post(db, project, rex.id if rex else None, f"Rex Research Agent: {rsummary}")

    for tid in order:
        t = tasks[tid]
        if t.assignee_actor_id is None:
            continue
        _announce_start(
            db, project, t, tasks, actors
        )  # agent acknowledges upstream, in the chat

        # structured handoff for every cross-department dependency edge
        for dep_id in t.depends_on:
            dep = tasks.get(dep_id)
            if (
                dep
                and dep.department_id
                and t.department_id
                and dep.department_id != t.department_id
            ):
                dep_art = artifacts_by_task.get(dep_id)
                communication.make_handoff(
                    db,
                    org_id=project.org_id,
                    project_id=project.id,
                    from_dept=dep.department_id,
                    to_dept=t.department_id,
                    context=f"{depts[dep.department_id].name} → {depts[t.department_id].name}: {t.goal}",
                    evidence=[dep_art.content] if dep_art else [],
                    sender_actor_id=dep.assignee_actor_id,
                )

        # the agent reads the team's shared context (upstream deliverables + project memory) first
        context = _gather_context(db, project, t, tasks, artifacts_by_task, depts)
        art = _run_and_review(db, project, t, critic, context=context)
        artifacts_by_task[t.id] = art
        if not art.needs_human and art.content:
            _remember(
                db, project, t, art, depts
            )  # Archivist: share it in project memory
            _announce_done(
                db, project, t, art, tasks, actors
            )  # agent reports back in the chat, hands off

        # Legal veto: a Legal task blocks any already-produced artifact with prohibited content.
        if depts.get(t.department_id) and depts[t.department_id].name == "Legal":
            for other in artifacts_by_task.values():
                v = review.legal_review(other.content)
                if not v.passed:
                    other.blocked, other.block_reason = True, "; ".join(v.reasons)

        t.status = "done" if not art.needs_human else "blocked"

    # a Legal-blocked artifact keeps its task (and the project) out of "done" — the veto actually blocks
    for tid, art in artifacts_by_task.items():
        if art.blocked:
            tasks[tid].status = "blocked"

    project.status = (
        "done" if all(x.status == "done" for x in tasks.values()) else "active"
    )
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
