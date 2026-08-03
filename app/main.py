from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import SessionLocal, init_db
from app.routers import (
    console, crm, governance, health, human, integrations, intelligence, orgs, portal, projects,
    runs, voice,
)


def _recover_stuck_executions() -> None:
    """A restart kills in-flight background executions; reset them to 'active' so they're retryable."""
    from sqlalchemy import select

    from app.models import Project

    db = SessionLocal()
    try:
        for pr in db.scalars(select(Project).where(Project.status == "executing")):
            pr.status = "active"
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _recover_stuck_executions()
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
