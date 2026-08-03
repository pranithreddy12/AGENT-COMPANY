"""End-to-end through the HTTP API: the Phase 0 gate itself."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register mappers; import before aliasing `app`)
from app.db import Base, get_db
from app.main import app as application


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)

    def _override():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    application.dependency_overrides[get_db] = _override
    with TestClient(application) as c:
        yield c
    application.dependency_overrides.clear()


def test_gate_run_and_trace(client):
    r = client.post("/orgs", json={"name": "Acme", "ceo_email": "ceo@acme.com", "ceo_password": "pw"})
    assert r.status_code == 200
    org = r.json()
    auth = {"Authorization": f"Bearer {org['access_token']}"}

    r = client.post("/runs", json={"actor_id": org["actor_id"], "input": "ping"}, headers=auth)
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["status"] == "succeeded"

    r = client.get(f"/runs/{run['id']}/trace", headers=auth)
    assert r.status_code == 200
    trace = r.json()
    assert [e["action"] for e in trace["events"]][0] == "run.started"
    assert trace["run"]["cost_usd"] == run["cost_usd"]


def test_run_requires_auth(client):
    r = client.post("/runs", json={"actor_id": "x", "input": "y"})
    assert r.status_code in (401, 403)


def test_project_gate(client):
    org = client.post("/orgs", json={"name": "Acme", "ceo_email": "c@a.com", "ceo_password": "pw"}).json()
    auth = {"Authorization": f"Bearer {org['access_token']}"}

    # goal -> reviewable DAG
    proj = client.post("/projects", json={"goal": "Build a widget"}, headers=auth).json()
    assert proj["status"] == "planning" and len(proj["tasks"]) == 7
    pid = proj["id"]

    # approve schedules it (critical path + dates)
    proj = client.post(f"/projects/{pid}/approve", headers=auth).json()
    assert proj["status"] == "active" and proj["critical_path"] and proj["due_at"]
    crit_before = proj["critical_path"]

    # execute kicks off in the background and returns immediately (execution correctness is
    # covered by the direct unit tests; the endpoint just starts it and guards double-runs)
    r = client.post(f"/projects/{pid}/execute", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "executing"

    # slip a parallel-branch task enough to steal the critical path
    t4 = next(t for t in proj["tasks"] if "test plan" in t["goal"].lower())
    proj = client.post(f"/tasks/{t4['id']}/slip", json={"added_hours": 6.0}, headers=auth).json()
    assert t4["id"] in proj["critical_path"]  # slip moved the critical path onto t4
    assert proj["critical_path"] != crit_before
