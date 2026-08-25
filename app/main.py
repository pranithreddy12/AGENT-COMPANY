from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import SessionLocal, init_db
from app.routers import (
    console, crm, governance, health, human, integrations, intelligence, orgs, portal, projects,
    runs, settings, teamchat, voice,
)


def recover_stuck(db) -> None:
    """A restart kills in-flight background work, leaving projects stuck mid-status. Reset them so
    they're never permanently stuck: an 'executing' project goes back to 'active' (re-runnable); a
    'generating' proposal goes to 'failed' with its dedup slot freed so a LeadForge retry regenerates
    (otherwise the client would poll a proposal that never finishes). Takes a session so it's testable."""
    from sqlalchemy import select

    from app.models import Project

    for pr in db.scalars(select(Project).where(Project.status == "executing")):
        pr.status = "active"
    for pr in db.scalars(select(Project).where(Project.status == "generating")):
        pr.status, pr.leadforge_lead_id = "failed", None
    db.commit()


def _recover_stuck_executions() -> None:
    db = SessionLocal()
    try:
        recover_stuck(db)
    finally:
        db.close()


def backfill_web_search(db) -> None:
    """web_search didn't exist when older orgs were created, so they have no ToolRegistration row for
    it and their worker profiles never got the grant. Idempotent: registers it per org (once) and
    grants it to any profile that already has the 'echo'+'get_time' worker shape but not web_search
    yet — new orgs get it at creation, this just catches the ones created before that existed."""
    from sqlalchemy import select

    from app.models import AgentProfile, Organization, ToolRegistration
    from app.services import tools

    for org in db.scalars(select(Organization)):
        has_reg = db.scalars(select(ToolRegistration).where(
            ToolRegistration.org_id == org.id, ToolRegistration.name == "web_search")).first()
        if has_reg is None:
            tools.register_builtins(db, org.id, ["web_search"])
        for prof in db.scalars(select(AgentProfile).where(AgentProfile.org_id == org.id)):
            grants = prof.tool_grants or []
            if "echo" in grants and "get_time" in grants and "web_search" not in grants:
                prof.tool_grants = [*grants, "web_search"]
    db.commit()


def _backfill_web_search() -> None:
    db = SessionLocal()
    try:
        backfill_web_search(db)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _recover_stuck_executions()
    _backfill_web_search()
    yield


app = FastAPI(title="Company OS", version="0.0.1-phase0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(orgs.router)
app.include_router(runs.router)
app.include_router(projects.router)
app.include_router(governance.router)
app.include_router(human.router)
app.include_router(console.router)
app.include_router(crm.router)
app.include_router(portal.router)
app.include_router(voice.router)
app.include_router(intelligence.router)
app.include_router(integrations.router)
app.include_router(teamchat.router)
app.include_router(settings.router)
