from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import init_db
from app.routers import (
    console, crm, governance, health, human, integrations, intelligence, orgs, portal, projects,
    runs, voice,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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
