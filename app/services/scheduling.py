"""Deterministic critical-path scheduler. Pure code — no DB, no LLM, no clock.

Operates on lightweight nodes: {"id": str, "effort": float, "deps": list[str]}.
The planning service converts ORM tasks to/from these. Hours are continuous from t=0
(the project start). Working-hours/timezone calendar is deferred.
# ponytail: continuous-hours model; swap in a working-hours calendar when humans join tasks.
"""
from dataclasses import dataclass

_EPS = 1e-9


class CycleError(Exception):
    pass


@dataclass
class Slot:
    est_start: float
    est_finish: float
    slack: float
    critical: bool


def topo_order(nodes: list[dict]) -> list[str]:
    ids = {n["id"] for n in nodes}
    indeg = {n["id"]: 0 for n in nodes}
    succ: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for n in nodes:
        for d in n["deps"]:
            if d not in ids:
                raise CycleError(f"task {n['id']} depends on unknown {d}")
            indeg[n["id"]] += 1
            succ[d].append(n["id"])
    queue = sorted([i for i, d in indeg.items() if d == 0])
    order = []
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for s in succ[cur]:
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
        queue.sort()
    if len(order) != len(nodes):
        raise CycleError("dependency cycle detected")
    return order


def schedule(nodes: list[dict]) -> tuple[dict[str, Slot], float, list[str]]:
    """Return (slots by id, project_finish, critical_path ordered by start)."""
    by_id = {n["id"]: n for n in nodes}
    order = topo_order(nodes)
    succ: dict[str, list[str]] = {i: [] for i in by_id}
    for n in nodes:
        for d in n["deps"]:
            succ[d].append(n["id"])

    # forward pass
    est_start, est_finish = {}, {}
    for i in order:
        n = by_id[i]
        est_start[i] = max((est_finish[d] for d in n["deps"]), default=0.0)
        est_finish[i] = est_start[i] + n["effort"]
    project_finish = max(est_finish.values(), default=0.0)

    # backward pass -> latest finish/start -> slack
    latest_finish = {}
    for i in reversed(order):
        outs = succ[i]
        latest_finish[i] = min((latest_finish[s] - by_id[s]["effort"] for s in outs), default=project_finish)
    slots = {}
    for i in order:
        ls = latest_finish[i] - by_id[i]["effort"]
        slack = ls - est_start[i]
        slots[i] = Slot(est_start[i], est_finish[i], round(slack, 6), slack <= _EPS)

    critical_path = sorted((i for i in order if slots[i].critical), key=lambda i: slots[i].est_start)
    return slots, project_finish, critical_path


def apply_slip(nodes: list[dict], task_id: str, added_hours: float) -> tuple[dict[str, Slot], float, list[str]]:
    """Increase a task's effort and reschedule. Mutates the matching node's effort in place."""
    for n in nodes:
        if n["id"] == task_id:
            n["effort"] = round(n["effort"] + added_hours, 6)
            break
    else:
        raise KeyError(task_id)
    return schedule(nodes)


def slip_risk(remaining_hours: float, est_effort: float, buffer: float = 1.2) -> bool:
    """At risk when the time left is less than the effort needed times a safety buffer."""
    return remaining_hours < est_effort * buffer
