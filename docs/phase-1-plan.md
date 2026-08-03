# Phase 1 — Work layer + the Lead

**Gate:** CEO states a goal → the Lead produces a reviewable task DAG (deps + dates) →
tasks execute → artifacts land → critical path recomputes on a simulated slip.

## New entities
- `departments` (name, charter, head_actor_id, budget)
- `projects` (goal, owner, start_at, due_at, status, health)
- `tasks` (project_id, goal, acceptance_criteria, assignee_actor_id, depends_on[], parent_task_id,
  est_effort_hours, status, priority + computed schedule fields: est_start_h, est_finish_h, slack_h, is_critical)
- `artifacts` (task_id, type, version, content, produced_by, status)

## The Lead
A planner **Actor**. `draft_project(goal)` runs a bounded provider call (audited as an AgentRun +
Events + cost), gets a structured task DAG, and materializes `proposed` tasks assigned to the
department whose charter covers each. **Plans and routes; never does the work.** EchoProvider
returns a deterministic Development DAG (a diamond, so slack + critical path are non-trivial);
Anthropic returns a real decomposition against the same schema. Invalid/cyclic plan → fail closed.

## Scheduling engine (`services/scheduling.py`) — pure, deterministic, no DB/LLM
- Topological sort (cycle → raise). Forward pass → est_start/est_finish. Backward pass → slack.
- Critical path = zero-slack chain. Project finish = max est_finish.
- `apply_slip(task, +hours)` → recompute downstream + critical path + project health.
- Slip risk: `time_remaining < est_effort × buffer`.
- Hours are continuous from `project.start_at`. `ponytail:` working-hours/timezone calendar deferred
  (agents run 24/7; human calendars matter in the human-layer phase).

## Flow / endpoints
- `POST /projects {goal}` → Lead drafts DAG, returns it for review (status `planning`, tasks `proposed`).
- `POST /projects/{id}/approve` → schedule, set due_at + critical flags, status `active`.
- `POST /projects/{id}/execute` → topological order; each task runs its Development agent (Phase 0
  executor) → Artifact lands, task `done`.
- `GET /projects/{id}` → tasks, deps, dates, critical path, health.
- `POST /tasks/{id}/slip {added_hours}` → recompute; downstream due_at + critical path + health shift.

## Tests
- scheduling: critical path, slack, cycle-raises, slip shifts the critical path.
- planning flow: draft → approve → execute → artifacts exist; the Lead assigns to Development, not itself.
- api: project gate end to end + slip recompute over HTTP.

## Deferred (named, not dropped)
Other 4 departments, HandoffPackets, thread budgets, Critic, Legal veto (Phase 2). Working-hours
calendar, capacity limits, escalation ladder (later). Real async execution / queue (Phase 2/3).
