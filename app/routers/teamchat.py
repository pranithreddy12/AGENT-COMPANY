"""Team group chat — assign work to agents by @mentioning them."""
import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sqlalchemy import select

from app.auth import Principal, current_principal, require_role
from app.db import get_db
from app.models import Department
from app.services import teamchat

router = APIRouter(tags=["teamchat"])


class ChatPost(BaseModel):
    message: str


@router.get("/teamchat")
def get_chat(db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> dict:
    """The team chat history plus the @mentionable roster (so the UI can autocomplete)."""
    depts = {d.id: d.name for d in db.scalars(select(Department).where(Department.org_id == p.org_id))}
    return {
        "messages": teamchat.history(db, p.org_id),
        "agents": [{"handle": teamchat.handle(a), "name": a.name or a.role, "role": a.role,
                    "department": depts.get(a.department_id)}
                   for a in teamchat.roster(db, p.org_id)],
    }


@router.post("/teamchat")
def post_chat(body: ChatPost, db: Session = Depends(get_db),
              p: Principal = Depends(require_role("ceo", "dept_head"))) -> dict:
    """Post to the team chat. Each @mention is classified first: a real task/goal starts real work
    in the background (the Lead drafts a real project, her actual job; anyone else gets a real Task
    run through agent -> Critic -> Legal) and posts its result back into the chat. A conversational
    message (question, status check, clarification) gets a plain in-character reply instead — no
    task machinery wasted on a message that was never asking for a deliverable. Returns as soon as
    work is queued so the UI never blocks on a model call."""
    result = teamchat.post(db, p.org_id, body.message, sender_actor_id=None)
    if result.get("error") == "empty_message":
        raise HTTPException(status_code=400, detail="message is empty")
    db.commit()  # persist message + queued work before the workers (own sessions) pick them up
    for t in result["tasks"]:
        if t["kind"] == "lead":
            threading.Thread(target=teamchat.run_chat_lead_in_background,
                             args=(p.org_id, t["actor_id"], t["goal"]), daemon=True).start()
        elif t["kind"] == "chat":
            threading.Thread(target=teamchat.run_chat_reply_in_background,
                             args=(p.org_id, t["actor_id"], t["goal"]), daemon=True).start()
        else:
            threading.Thread(target=teamchat.run_chat_task_in_background,
                             args=(t["task_id"],), daemon=True).start()
    return result
