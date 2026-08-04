"""Ask an agent anything — status, an update, a question. The agent answers in first person,
grounded in its real work (its deliverables, scorecard, and the project it's on)."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Actor, AgentProfile, Artifact, Department, MemoryRecord, Task
from app.services import intelligence, playbooks
from app.services.llm import build_provider


def ask_agent(db: Session, org_id: str, actor: Actor, question: str, project_id: str | None = None) -> str:
    prof = db.get(AgentProfile, actor.agent_profile_id) if actor.agent_profile_id else None
    dept = db.get(Department, actor.department_id) if actor.department_id else None
    role_desc = dept.charter if dept else "You are the Chief of Staff — you plan and route work."
    pb = playbooks.active(db, org_id, actor.department_id) if actor.department_id else None
    sc = intelligence.scorecard(db, org_id, actor)

    recent = list(db.scalars(select(Artifact).where(Artifact.produced_by_actor_id == actor.id)
                             .order_by(Artifact.created_at.desc())))[:3]
    recent_txt = "\n".join(f"- {a.task_id}: {' '.join(a.content.split())[:180]}" for a in recent) or "(nothing delivered yet)"

    # the agent's own task memory: what's done, what's still to do
    mine = list(db.scalars(select(Task).where(Task.assignee_actor_id == actor.id)))
    todo = [t.goal for t in mine if t.status != "done"]
    completed = [t.goal for t in mine if t.status == "done"]

    mem_txt = ""
    if project_id:
        mem = list(db.scalars(select(MemoryRecord).where(
            MemoryRecord.project_id == project_id, MemoryRecord.scope == "project")))
        mem_txt = "\n".join(m.content for m in mem[-8:])

    system = (
        f"You are {actor.name}, the {dept.name if dept else 'Lead'} agent at an AI-run agency. {role_desc} "
        "Answer the human directly, in first person, concise and specific. If asked for status or an update, "
        "summarize what you've completed and what's next. Ground every answer in your actual work below — "
        "do not invent."
    )
    user = (
        f"My status: {sc['tasks_completed']} tasks completed, {round(sc['first_pass_rate'] * 100)}% first-pass, "
        f"{sc['runs']} runs.\n"
        f"My completed tasks: {'; '.join(completed) or '(none yet)'}\n"
        f"My to-do (still open): {'; '.join(todo) or '(nothing pending)'}\n"
        f"My recent deliverables:\n{recent_txt}\n"
        + (f"\nWhat my team knows on this project:\n{mem_txt}\n" if mem_txt else "")
        + (f"\nMy department playbook:\n{pb.markdown}\n" if pb else "")
        + f"\nThe human asks: {question}"
    )
    provider = build_provider(prof.provider, prof.model, settings.anthropic_api_key) if prof else None
    if provider is None:
        return "I have no profile configured."
    comp = provider.complete(system=system, messages=[{"role": "user", "content": user}], tools=[], max_tokens=512)
    return (comp.text or "").strip() or "(no response)"
