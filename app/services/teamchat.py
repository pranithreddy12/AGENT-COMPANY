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
from app.models import Actor, Artifact, Department, Message, Project, Task, Thread
from app.services import communication, planning

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
    return list(db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.type == "agent")))


def team_thread(db: Session, org_id: str) -> Thread:
    """The org's single team chat thread (find-or-create)."""
    t = db.scalars(select(Thread).where(Thread.org_id == org_id,
                                        Thread.thread_type == TEAM_THREAD_TYPE)).first()
    if t is None:
        t = communication.create_thread(db, org_id, TEAM_THREAD_TYPE, "Team chat",
                                        message_budget=_NO_BUDGET)
    return t


def _chat_project(db: Session, org_id: str) -> Project:
    """Tasks need a project to hang on; chat-assigned work shares one per org."""
    p = db.scalars(select(Project).where(Project.org_id == org_id,
                                         Project.goal == _CHAT_PROJECT_GOAL)).first()
    if p is None:
        p = Project(org_id=org_id, goal=_CHAT_PROJECT_GOAL, status="active", health="on_track")
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


def post(db: Session, org_id: str, text: str, sender_actor_id: str | None = None) -> dict:
    """Post a human message to the team chat. Every @mentioned agent gets a real Task assigned.
    Returns the message + the ids of tasks created (the caller runs them in the background)."""
    text = (text or "").strip()
    if not text:
        return {"error": "empty_message"}
    thread = team_thread(db, org_id)
    msg = communication.post_message(db, thread, sender_actor_id, text)
    mentioned = parse_mentions(text, roster(db, org_id))
    if not mentioned:
        return {"message_id": msg.id, "tasks": []}  # plain chatter, no work assigned

    project = _chat_project(db, org_id)
    goal = _MENTION_RE.sub("", text).strip() or text  # the instruction minus the @handles
    tasks = []
    for agent in mentioned:
        t = Task(org_id=org_id, project_id=project.id, goal=goal, department_id=agent.department_id,
                 assignee_actor_id=agent.id, status="in_progress", est_effort_hours=1.0)
        db.add(t)
        db.flush()
        tasks.append({"task_id": t.id, "agent": agent.name, "handle": handle(agent)})
    return {"message_id": msg.id, "tasks": tasks}


def run_chat_task_in_background(task_id: str) -> None:
    """Execute one chat-assigned task in its own session/thread, then post the result back into the
    chat as the agent. Any failure is reported in-chat — a silent failure would leave the human
    waiting on work that will never arrive."""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if task is None:
            return
        project = db.get(Project, task.project_id)
        agent = db.get(Actor, task.assignee_actor_id)
        try:
            art = planning.rerun_task(db, project, task)   # agent -> Critic -> Legal, commits
        except Exception as e:
            task.status = "blocked"
            _reply(db, task.org_id, agent, f"I couldn't finish that: {type(e).__name__}. "
                                           f"The task is marked blocked for a human to look at.")
            db.commit()
            return
        _reply(db, task.org_id, agent, _summary(art))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _summary(art: Artifact) -> str:
    """What the agent says back in chat: flags first, then the work itself."""
    if art.blocked:
        return f"Legal blocked this: {art.block_reason}. Nothing sent — a human needs to clear it."
    head = "Done — needs a human review before it goes anywhere.\n\n" if art.needs_human else "Done.\n\n"
    body = (art.content or "").strip()
    return head + (body if len(body) <= 1500 else body[:1500] + "\n\n[...truncated — open the artifact for the full text]")


def _reply(db: Session, org_id: str, agent: Actor | None, content: str) -> None:
    thread = team_thread(db, org_id)
    communication.post_message(db, thread, agent.id if agent else None, content)


def history(db: Session, org_id: str, limit: int = 200) -> list[dict]:
    """The chat, oldest first, with sender names resolved for rendering."""
    thread = team_thread(db, org_id)
    msgs = list(db.scalars(select(Message).where(Message.thread_id == thread.id)
                           .order_by(Message.created_at.desc()).limit(limit)))[::-1]
    actors = {a.id: a for a in db.scalars(select(Actor).where(Actor.org_id == org_id))}
    depts = {d.id: d.name for d in db.scalars(select(Department).where(Department.org_id == org_id))}
    out = []
    for m in msgs:
        a = actors.get(m.sender_actor_id) if m.sender_actor_id else None
        out.append({"id": m.id, "content": m.content, "at": m.created_at.isoformat(),
                    "sender": (a.name or a.role) if a else "You",
                    "is_agent": bool(a and a.type == "agent"),
                    "department": depts.get(a.department_id) if a else None})
    return out
