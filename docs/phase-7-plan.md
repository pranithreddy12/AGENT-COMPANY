# Phase 7 — Intelligence & hardening

Closes the build. No single gate sentence in the brief; the north star is Section 13 — a CEO opens
one screen, the company improves itself, every artifact has an audit trail.

## Features (deterministic; models write/judge, code measures)
- **Scorecards** — per-actor metrics from the audit substrate: tasks_completed, completion_rate,
  rework_rate (revised artifacts), first_pass_rate (Critic passed on attempt 1), escalation_rate,
  blocked_rate, cost_per_task. Persisted snapshots. `best_agent()` drives auto-routing to the top performer.
- **Hire-an-agent** — a job description generates a draft `AgentProfile`, runs it against a small eval
  set, and reports results **before** hiring; confirm creates the Actor. Nothing hired unvetted.
- **Retro agent** — scans recent events/artifacts/runs for recurring failure modes (escalations,
  rework, blocks, failed runs) and **proposes Playbook amendments** (drafts) for the affected
  departments — which flow through the Phase 4 activate gate. The company improves via SOPs.
- **Playbook A/B** — artifacts record the Playbook version that produced them; `ab_compare()` compares
  first_pass/rework between two versions of a department's Playbook.

## Hardening
- **Failure injection** — a provider that raises mid-run fails the task closed (blocked), the project
  does not complete, and nothing crashes or half-commits. Test asserts it.
- **Deployment** — a minimal `Dockerfile` (uvicorn) + notes; Postgres via `DATABASE_URL`.

## Endpoints
`GET /scorecards` · `GET /scorecards/{actor_id}` · `POST /hire` · `POST /hire/{profile_id}/confirm` ·
`POST /retro` · `GET /playbooks/ab?department_id&a&b`.

## Tests
- scorecards compute the right rates from a run project.
- hire → eval results → confirm creates a working agent.
- retro finds an injected failure mode and proposes an amendment.
- failure injection: provider error → task blocked, project not done, no crash.

## Deferred (named)
Real eval sets from historical tasks (the seam takes any task list). Load testing at scale (needs a
perf env). Next.js/shadcn console (thin HTML stands in). LLM-authored retro narratives behind the seam.
