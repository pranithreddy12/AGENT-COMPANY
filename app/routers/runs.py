"""Trigger a bounded agent run and read back its complete, costed, replayable trace."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal
from app.db import get_db
from app.models import Actor, AgentRun
from app.schemas import EventOut, RunCreate, RunOut, TraceOut
from app.services import events, runs
from app.tenancy import Tenant

router = APIRouter(tags=["runs"])


def _run_out(run: AgentRun) -> RunOut:
    return RunOut(
        id=run.id, trace_id=run.trace_id, status=run.status, turns_used=run.turns_used,
        cost_usd=run.cost_usd, result=run.result, error=run.error,
    )


@router.post("/runs", response_model=RunOut)
def start_run(body: RunCreate, db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> RunOut:
    tenant = Tenant(db, p.org_id)
    actor = tenant.get(Actor, body.actor_id)
    if actor is None:
        raise HTTPException(status_code=404, detail="actor not found")
    run = runs.create_run(db, p.org_id, actor, body.input)
    run = runs.execute(db, run)  # synchronous for Phase 0
    return _run_out(run)


@router.get("/runs/{run_id}/trace", response_model=TraceOut)
def get_trace(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> TraceOut:
    tenant = Tenant(db, p.org_id)
    run = tenant.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    evs = events.trace(db, run.trace_id)
    return TraceOut(
        run=_run_out(run),
        events=[
            EventOut(
                seq=e.seq, action=e.action, target=e.target, before=e.before, after=e.after,
                cost_usd=e.cost_usd, latency_ms=e.latency_ms,
            )
            for e in evs
        ],
    )
