"""Every GET endpoint the console calls must actually work over real HTTP with dependency
injection resolved — not just import cleanly. A NameError from a bad edit (e.g. a name dropped from
an import line) only surfaces when the function actually RUNS, so `import app.main` succeeding
proves nothing about this. This test would have caught the AgentRun import regression immediately
instead of it only showing up as a live 500 on /activity."""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.db import Base, get_db
from app.main import app
from app.routers.orgs import create_org
from app.schemas import OrgCreate

GET_ENDPOINTS = [
    "/departments", "/console/standup", "/approvals", "/projects", "/agents",
    "/activity", "/activity?limit=6", "/leads", "/teamchat", "/settings/llm",
]


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def _override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app), Session()


def test_every_console_get_endpoint_returns_200_not_500():
    client, db = _client()
    try:
        r = create_org(OrgCreate(name="Smoke Co", ceo_email="smoke@a.com", ceo_password="pw"), db)
        db.commit()
        headers = {"authorization": f"Bearer {r.access_token}"}
        failures = []
        for path in GET_ENDPOINTS:
            resp = client.get(path, headers=headers)
            if resp.status_code >= 500:
                failures.append(f"{path} -> {resp.status_code}: {resp.text[:200]}")
        assert not failures, "\n".join(failures)
    finally:
        app.dependency_overrides.clear()
        db.close()
