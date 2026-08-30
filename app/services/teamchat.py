"""Team group chat: one org-wide thread where a human assigns work by @mentioning agents.

@mentioning an agent creates a real Task assigned to it and runs it through the SAME machinery as
any other task (department agent -> Critic -> Legal veto -> Artifact), then the agent posts its
result back into the chat. So the chat is a task queue with the governance intact, not a talk shop:
anything an agent produces here is still reviewable, still Legal-screened, still audited.
"""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Actor, AgentProfile, Artifact, Department, Message, Project, Task, Thread
from app.services import communication, llm, planning

TEAM_THREAD_TYPE = "team"
_CHAT_PROJECT_GOAL = "Team chat requests"
# a human-driven chat must never hit the agent-loop budget guard, so effectively no ceiling
_NO_BUDGET = 10**9
_MENTION_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_-]*)")


def handle(actor: Actor) -> str:
    """Mention handle for an agent: its first name, lowercased ('Cleo Client Agent' -> 'cleo')."""
    return (actor.name or actor.role or "agent").strip().split()[0].lower()


def roster(db: Session, org_id: str) -> list[Actor]:
    """Every agent in the org that can be @mentioned."""
    return list(
        db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.type == "agent"))
    )


def team_thread(db: Session, org_id: str) -> Thread:
    """The org's single team chat thread (find-or-create)."""
    t = db.scalars(
        select(Thread).where(
            Thread.org_id == org_id, Thread.thread_type == TEAM_THREAD_TYPE
        )
    ).first()
    if t is None:
        t = communication.create_thread(
            db, org_id, TEAM_THREAD_TYPE, "Team chat", message_budget=_NO_BUDGET
        )
    return t


def _chat_project(db: Session, org_id: str) -> Project:
    """Tasks need a project to hang on; chat-assigned work shares one per org."""
    p = db.scalars(
        select(Project).where(
            Project.org_id == org_id, Project.goal == _CHAT_PROJECT_GOAL
        )
    ).first()
    if p is None:
        p = Project(
            org_id=org_id, goal=_CHAT_PROJECT_GOAL, status="active", health="on_track"
        )
        db.add(p)
        db.flush()
    return p


def parse_mentions(text: str, agents: list[Actor]) -> list[Actor]:
    """Resolve @handles in order of first appearance, deduped. Unknown handles are ignored."""
    by_handle = {handle(a): a for a in agents}
    out, seen = [], set()
    for raw in _MENTION_RE.findall(text or ""):
        a = by_handle.get(raw.lower())
        if a is not None and a.id not in seen:
            seen.add(a.id)
            out.append(a)
    return out


def classify_intent(db: Session, org_id: str, agent: Actor, text: str, transcript: str) -> str:
    """Is this @mention asking for real work — a new goal/deliverable to actually produce or execute
    (or, for the Lead, decompose into a project) — or is it conversational: a question, a status
    check, a clarification, or an instruction that isn't itself a concrete deliverable? Not every
    message that mentions an agent is a task; forcing all of them through task machinery is what
    produced confusing internal errors (e.g. "PlanError") leaking into the chat for messages that
    were never actually a piece of work.

    Classified by the agent's own configured model. Echo (deterministic, zero real reasoning) always
    classifies as "task" — that's not a cop-out, it's honest: Echo can't classify natural-language
    intent at all, and "always task" is this path's original, already-tested contract for the
    zero-cost demo/test provider. Real classification only matters once a real model is configured —
    which is exactly where the bug this fixes was actually hit. Fails safe to "chat" on any
    classification error: if we can't even tell what was asked, a plain reply is cheaper and safer
    than burning a Critic+Legal cycle (or a plan attempt) on a guess."""
    prof = db.get(AgentProfile, agent.agent_profile_id) if agent.agent_profile_id else None
    if prof is None or prof.provider == "echo":
        return "task"
    try:
        provider = llm.build_provider(prof.provider, prof.model, llm.resolve_api_key(db, org_id, prof.provider))
        system = (
            "Classify the human's latest message in this team chat as exactly one word.\n"
            "TASK: a new goal, deliverable, or piece of work to actually produce, execute, or plan.\n"
            "CHAT: a question, status check, clarification, discussion, or an instruction that isn't "
            "itself a concrete deliverable to produce.\n"
            "Reply with exactly one word: TASK or CHAT."
        )
        user = f"Recent conversation:\n{transcript}\n\nClassify this latest message: {text}"
        comp = provider.complete(system=system, messages=[{"role": "user", "content": user}], tools=[], max_tokens=5)
        verdict = (comp.text or "").strip().upper()
        return "chat" if "CHAT" in verdict and "TASK" not in verdict else "task"
    except Exception:
        return "chat"


def post(
    db: Session, org_id: str, text: str, sender_actor_id: str | None = None
) -> dict:
    """Post a human message to the team chat. Each @mentioned agent's message is classified first
    (classify_intent): a real task/goal gets real work assigned — the Lead gets a real drafted
    project (planning.draft_project, the same thing the Dashboard's "Plan it" directive calls), any
    other agent gets a real Task run through Critic + Legal. A conversational message (question,
    status check, clarification) gets a plain in-character reply instead — no Task, no Critic, no
    Legal, no wasted plan attempt. Returns the message + what to run in the background per mention."""
    text = (text or "").strip()
    if not text:
        return {"error": "empty_message"}
    thread = team_thread(db, org_id)
    msg = communication.post_message(db, thread, sender_actor_id, text)
    mentioned = parse_mentions(text, roster(db, org_id))
    if not mentioned:
        return {"message_id": msg.id, "tasks": []}  # plain chatter, no work assigned

    project = _chat_project(db, org_id)
    goal = (
        _MENTION_RE.sub("", text).strip() or text
    )  # the instruction minus the @handles
    transcript = recent_transcript(db, org_id)
    tasks = []
    for agent in mentioned:
        intent = classify_intent(db, org_id, agent, goal, transcript)
        if intent == "chat":
            tasks.append(
                {
                    "kind": "chat",
                    "actor_id": agent.id,
                    "goal": goal,
                    "agent": agent.name,
                    "handle": handle(agent),
                }
            )
            continue
        if agent.role == "lead":
            tasks.append(
                {
                    "kind": "lead",
                    "actor_id": agent.id,
                    "goal": goal,
                    "agent": agent.name,
                    "handle": handle(agent),
                }
            )
            continue
        t = Task(
            org_id=org_id,
            project_id=project.id,
            goal=goal,
            department_id=agent.department_id,
            assignee_actor_id=agent.id,
            status="in_progress",
            est_effort_hours=1.0,
        )
        db.add(t)
        db.flush()
        tasks.append(
            {
                "kind": "task",
                "task_id": t.id,
                "agent": agent.name,
                "handle": handle(agent),
            }
        )
    return {"message_id": msg.id, "tasks": tasks}


def run_chat_lead_in_background(org_id: str, lead_actor_id: str, goal: str) -> None:
    """@mentioning the Lead: draft a REAL project (planning.draft_project — a Project + a scheduled
    task DAG across departments), then summarize it back into chat. This is the Lead's actual
    specialized function — routing her through the generic single-task executor (like any other
    agent) would just produce a chatbot-style reply, since her real decomposition logic lives in
    provider.plan(), not the generic completion path a Task runs through."""
    db = SessionLocal()
    try:
        agent = db.get(Actor, lead_actor_id)
        try:
            proj, drafted = planning.draft_project(db, org_id, goal)
            db.commit()
        except Exception as e:
            db.rollback()
            _reply(
                db,
                org_id,
                agent,
                f"I couldn't draft a plan for that: {type(e).__name__}.",
            )
            db.commit()
            return
        depts = {
            d.id: d.name
            for d in db.scalars(select(Department).where(Department.org_id == org_id))
        }
        flow: list[str] = []
        for t in drafted:  # department sequence in plan order, deduped
            name = depts.get(t.department_id)
            if name and name not in flow:
                flow.append(name)
        first = " ".join(drafted[0].goal.split()) if drafted else ""
        _reply(
            db,
            org_id,
            agent,
            f"Plan drafted — {len(drafted)} task{'s' if len(drafted) != 1 else ''} across "
            f"{len(flow)} department{'s' if len(flow) != 1 else ''}"
            + (f" ({' → '.join(flow)})" if flow else "")
            + (f", starting with “{first[:80]}”." if first else ".")
            + " Review and approve it from Projects and I'll put the team on it.",
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def run_chat_reply_in_background(org_id: str, actor_id: str, text: str) -> None:
    """A conversational reply for an @mention classified as "chat" — no Task, no Critic, no Legal
    review. The agent answers in character, grounded in the real conversation, instead of the
    request being forced through task machinery it was never actually asking for."""
    db = SessionLocal()
    try:
        agent = db.get(Actor, actor_id)
        if agent is None:
            return
        prof = db.get(AgentProfile, agent.agent_profile_id) if agent.agent_profile_id else None
        dept = db.get(Department, agent.department_id) if agent.department_id else None
        role_desc = dept.charter if dept else "You are the Chief of Staff — you plan and route work."
        transcript = recent_transcript(db, org_id)
        if prof is None:
            _reply(db, org_id, agent, "I don't have a model configured to answer that.")
            db.commit()
            return
        try:
            provider = llm.build_provider(prof.provider, prof.model, llm.resolve_api_key(db, org_id, prof.provider))
            system = (
                f"You are {agent.name}, the {dept.name if dept else 'Lead'} agent at an AI-run "
                f"agency. {role_desc} Reply directly and briefly, in first person, grounded in the "
                "real conversation below. This is a chat answer, not a deliverable — do not produce "
                "a formal document; just answer what was actually asked."
            )
            user = f"Recent conversation:\n{transcript}\n\nRespond to the latest message."
            comp = provider.complete(system=system, messages=[{"role": "user", "content": user}],
                                     tools=[], max_tokens=400)
            reply = (comp.text or "").strip() or "(no response)"
        except Exception as e:
            reply = f"I couldn't answer that: {type(e).__name__}."
        _reply(db, org_id, agent, reply)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


_TRANSCRIPT_MESSAGES = (
    12  # recent chat messages an agent reads so its answer fits the conversation
)
_TRANSCRIPT_LINE_CAP = 300  # chars per quoted message


def recent_transcript(
    db: Session, org_id: str, limit: int = _TRANSCRIPT_MESSAGES
) -> str:
    """The last `limit` messages of the team chat, oldest first, as readable lines. This is how a
    chat-assigned agent sees the conversation around the request (clarifications, related asks,
    co-mentioned teammates) instead of just its own stripped one-liner."""
    thread = team_thread(db, org_id)
    msgs = list(
        db.scalars(
            select(Message)
            .where(Message.thread_id == thread.id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    )[::-1]
    agents = {a.id: a for a in roster(db, org_id)}
    lines = []
    for m in msgs:
        a = agents.get(m.sender_actor_id) if m.sender_actor_id else None
        who = (a.name or a.role) if a else "the human"
        body = " ".join(str(m.content).split())[:_TRANSCRIPT_LINE_CAP]
        lines.append(f"{who}: {body}")
    return "\n".join(lines)


def run_chat_task_in_background(task_id: str) -> None:
    """Execute one chat-assigned task in its own session/thread, then post the result back into the
    chat as the agent. Any failure is reported in-chat — a silent failure would leave the human
    waiting on work that will never arrive.

    The agent works from the actual conversation (recent_transcript) rather than the shared chat
    project's memory, which would mix every past unrelated request into this answer."""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if task is None:
            return
        project = db.get(Project, task.project_id)
        agent = db.get(Actor, task.assignee_actor_id)
        transcript = recent_transcript(db, task.org_id)
        try:
            art = planning.rerun_task(
                db,
                project,
                task,
                extra_context=(
                    "The team-chat conversation this request came from "
                    "(your request is the most recent ask):\n" + transcript
                )
                if transcript
                else "",
                include_memory=False,
            )  # agent -> Critic -> Legal, commits
        except Exception as e:
            task.status = "blocked"
            _reply(
                db,
                task.org_id,
                agent,
                f"I couldn't finish that: {type(e).__name__}. "
                f"The task is marked blocked for a human to look at.",
            )
            db.commit()
            return
        _reply(db, task.org_id, agent, _summary(art, request=task.goal))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


_MAX_CHAT_REPLY = 8000  # generous headroom over a typical ~3000-token deliverable; guards only pathological output


def _summary(art: Artifact, request: str | None = None) -> str:
    """What the agent says back in chat: an acknowledgment of the actual ask, flags first, then the
    work itself. The chat UI renders this as markdown and lets the human expand long replies, so
    this only needs to protect against a truly runaway artifact — not clip normal deliverables."""
    if art.blocked:
        return f"Legal blocked this: {art.block_reason}. Nothing sent — a human needs to clear it."
    opener = ""
    if request:
        req = " ".join(request.split())[:120]
        opener = f"You asked me to “{req}” — here's my work on it.\n\n"
    head = (
        "Done — needs a human review before it goes anywhere.\n\n"
        if art.needs_human
        else "Done.\n\n"
    )
    body = (art.content or "").strip()
    if len(body) > _MAX_CHAT_REPLY:
        body = (
            body[:_MAX_CHAT_REPLY]
            + "\n\n*(cut off — this artifact ran unusually long)*"
        )
    return opener + head + body


def _reply(db: Session, org_id: str, agent: Actor | None, content: str) -> None:
    thread = team_thread(db, org_id)
    communication.post_message(db, thread, agent.id if agent else None, content)


def history(db: Session, org_id: str, limit: int = 200) -> list[dict]:
    """The chat, oldest first, with sender names resolved for rendering."""
    thread = team_thread(db, org_id)
    msgs = list(
        db.scalars(
            select(Message)
            .where(Message.thread_id == thread.id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    )[::-1]
    actors = {a.id: a for a in db.scalars(select(Actor).where(Actor.org_id == org_id))}
    depts = {
        d.id: d.name
        for d in db.scalars(select(Department).where(Department.org_id == org_id))
    }
    out = []
    for m in msgs:
        a = actors.get(m.sender_actor_id) if m.sender_actor_id else None
        out.append(
            {
                "id": m.id,
                "content": m.content,
                "at": m.created_at.isoformat(),
                "sender": (a.name or a.role) if a else "You",
                "is_agent": bool(a and a.type == "agent"),
                "department": depts.get(a.department_id) if a else None,
            }
        )
    return out
