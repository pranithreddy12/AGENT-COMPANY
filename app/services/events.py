"""Append-only Event writer + replay reader. Events are the source of truth.

No update or delete path — only append() and reads.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AgentRun, Event


def _next_seq(db: Session, trace_id: str) -> int:
    current = db.scalar(select(func.max(Event.seq)).where(Event.trace_id == trace_id))
    return (current or 0) + 1


def append(
    db: Session,
    *,
    org_id: str,
    trace_id: str,
    action: str,
    run_id: str | None = None,
    actor_id: str | None = None,
    target: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
    simulated: bool = False,
) -> Event:
    ev = Event(
        org_id=org_id,
        trace_id=trace_id,
        run_id=run_id,
        actor_id=actor_id,
        seq=_next_seq(db, trace_id),
        action=action,
        target=target,
        before=before,
        after=after,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        simulated=simulated,
    )
    db.add(ev)
    db.flush()
    return ev


def trace(db: Session, trace_id: str) -> list[Event]:
    return list(db.scalars(select(Event).where(Event.trace_id == trace_id).order_by(Event.seq)))


def reconstruct(events: list[Event]) -> dict:
    """Fold the event stream back into run state — proves the log is sufficient."""
    status = None
    result = None
    cost = 0.0
    for ev in events:
        cost += ev.cost_usd
        if ev.action.startswith("run."):
            status = ev.action.split(".", 1)[1]  # started|succeeded|failed|killed
            if ev.after and "result" in ev.after:
                result = ev.after["result"]
    return {"status": status, "result": result, "cost_usd": round(cost, 6)}


def replay_matches(db: Session, run: AgentRun) -> bool:
    state = reconstruct(trace(db, run.trace_id))
    return (
        state["status"] == run.status
        and abs(state["cost_usd"] - run.cost_usd) < 1e-6
        and state["result"] == run.result
    )
