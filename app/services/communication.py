"""Structured communication: threads with enforced message budgets, and handoff packets.

The message budget is the primary defense against token-burning loops: posting past budget
without resolution flips the thread to `escalated` and refuses the message.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import HandoffPacket, Message, Thread


class BudgetExceeded(Exception):
    pass


def create_thread(db: Session, org_id: str, thread_type: str, subject: str,
                  project_id: str | None = None, message_budget: int = 6) -> Thread:
    t = Thread(org_id=org_id, project_id=project_id, thread_type=thread_type,
               subject=subject, message_budget=message_budget)
    db.add(t)
    db.flush()
    return t


def post_message(db: Session, thread: Thread, sender_actor_id: str | None, content: str) -> Message:
    count = db.scalar(select(func.count(Message.id)).where(Message.thread_id == thread.id)) or 0
    if thread.status == "open" and count >= thread.message_budget:
        thread.status = "escalated"  # auto-escalate: budget hit without resolution
        db.flush()
        raise BudgetExceeded(f"thread {thread.id} hit its {thread.message_budget}-message budget")
    m = Message(org_id=thread.org_id, thread_id=thread.id, sender_actor_id=sender_actor_id, content=content)
    db.add(m)
    db.flush()
    return m


def make_handoff(db: Session, *, org_id: str, project_id: str, from_dept: str | None, to_dept: str | None,
                 context: str, evidence: list[str], open_questions: list[str] | None = None,
                 confidence: float = 1.0, sender_actor_id: str | None = None) -> HandoffPacket:
    """Record a structured cross-team transfer and open a handoff thread with one message."""
    packet = HandoffPacket(
        org_id=org_id, project_id=project_id, from_department_id=from_dept, to_department_id=to_dept,
        context=context, evidence=evidence, open_questions=open_questions or [], confidence=confidence,
    )
    db.add(packet)
    thread = create_thread(db, org_id, "handoff", subject=context, project_id=project_id)
    post_message(db, thread, sender_actor_id, f"Handoff: {context}. Evidence: {len(evidence)} item(s).")
    db.flush()
    return packet
