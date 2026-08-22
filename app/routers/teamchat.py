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
    """Post to the team chat. Every @mentioned agent gets a real Task and starts working on it in
    the background (agent -> Critic -> Legal), then posts its result back into the chat. Returns as
    soon as the tasks are queued so the UI never blocks on a model call."""
    result = teamchat.post(db, p.org_id, body.message, sender_actor_id=None)
    if result.get("error") == "empty_message":
        raise HTTPException(status_code=400, detail="message is empty")
    db.commit()  # persist message + tasks before the workers (own sessions) pick them up
    for t in result["tasks"]:
        threading.Thread(target=teamchat.run_chat_task_in_background,
                         args=(t["task_id"],), daemon=True).start()
    return result
