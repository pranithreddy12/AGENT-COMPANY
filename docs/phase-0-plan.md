# Phase 0 — Foundations (architecture & build plan)

**Gate ("done when"):** trigger a trivial agent run from an API call and get back a
complete, replayable trace with cost attached.

Everything here serves that one demoable outcome. No speculative infra. The schema is
shaped so later phases extend it, but we only build the tables Phase 0 needs.

---

## Confirmed decisions (Section 12)

1. Buyer = **agency running client work** → client-facing agents are load-bearing (later phases).
2. **Multi-tenant schema, single-org UX.** `org_id` on every row, enforced in a shared query layer.
3. Development = **specs-and-reviews** for v1. Tool interface designed so sandboxed code exec slots in later.

## Infra decision for early phases (lean)

Start with **Postgres + FastAPI + a synchronous run executor**. No Redis/Celery/WebSockets/
LiveKit/pgvector yet — the schema anticipates them, but they're dead weight until a phase has
async work, live push, embeddings, or voice to justify them.

- Redis+Celery → arrives Phase 2/3 when runs need queuing/priority tiers.
- WebSockets/SSE → Phase 1 when there's a live task board.
- pgvector → Phase 3/5 when scoped memory needs embeddings.
- LiveKit/Twilio/Deepgram → Phase 6 (voice).

> `ponytail:` synchronous executor now; swap the executor's `.run()` behind a queue when a
> phase actually needs concurrency. State lives in DB rows, so the swap is local.

---

## Stack (Phase 0)

- Python 3.12, **FastAPI**, **Pydantic v2** (schemas everywhere).
- **SQLAlchemy 2.0** (typed) + **Alembic** migrations.
- **PostgreSQL** (plain; pgvector added later).
- Auth: JWT bearer, roles `ceo | dept_head | member | client`. Minimal — password hash +
  token issue. No OAuth/SSO in Phase 0.
- LLM: provider-abstracted. Two providers: `EchoProvider` (deterministic, for tests/demos,
  zero cost) and `AnthropicProvider` (real). Selected per AgentProfile.
- Tests: pytest. Focus on tenant isolation, event replay, cost math.
- **No frontend in Phase 0** — the gate is API-triggered. UI starts Phase 1.

---

## Module layout

```
company_os/
  app/
    main.py               # FastAPI app, router wiring
    config.py             # settings (env), DB URL, provider keys
    db.py                 # engine, session, Base
    tenancy.py            # shared query layer: scoped session, cross-tenant guard
    auth.py               # JWT, password hash, role dependency
    models/               # SQLAlchemy models (Phase 0 subset)
      org.py  actor.py  agent_profile.py  tool.py  run.py  event.py
    schemas/              # Pydantic request/response models
    services/
      events.py           # append-only Event writer + replay reader
      cost.py             # cost accounting (deterministic, code not model)
      tools/
        registry.py       # tool registration + per-agent grants + side-effect class
        builtin.py        # 1-2 trivial read tools for the demo run
      llm/
        base.py           # Provider protocol: complete(messages, tools, budget) -> Result
        echo.py           # EchoProvider
        anthropic.py      # AnthropicProvider (bounded: max_tokens, timeout)
      runs/
        state_machine.py  # DB-backed run FSM
        executor.py       # bounded turn loop, writes Events, enforces caps
    routers/
      health.py  orgs.py  actors.py  runs.py
  alembic/                # migrations
  tests/
```

---

## Data model (Phase 0 subset)

Only the tables the gate needs. Full 20-entity model lands across later phases.

| Table | Purpose | Key fields |
|---|---|---|
| `organizations` | tenant | id, name, plan, cost_cap_usd, timezone, working_hours(json) |
| `users` | login identity | id, org_id, email, pw_hash, role |
| `actors` | unified worker | id, org_id, type(agent\|human), role, status, user_id(nullable), agent_profile_id(nullable) |
| `agent_profiles` | agent config | id, org_id, system_prompt, model, provider, max_turns, max_tokens, cost_ceiling_usd, autonomy_default, tool_grants(json) |
| `tool_registrations` | central registry | id, org_id, name, schema(json), side_effect(read\|write\|irreversible), cost_estimate_usd |
| `agent_runs` | bounded execution | id, org_id, actor_id, trigger, status, turns_used, cost_usd, started_at, ended_at, trace_id, result(json), error |
| `events` | **append-only** audit | id, org_id, trace_id, run_id(nullable), actor_id, action, target, before(json), after(json), cost_usd, latency_ms, created_at |

Rules:
- `events` is insert-only. No update/delete path in the ORM layer. Source of truth.
- Every table has `org_id`. The scoped session (`tenancy.py`) injects an `org_id` filter and a
  test asserts a cross-tenant read returns nothing.
- `cost_usd` is computed in `services/cost.py` from token counts × a per-model rate table.
  Deterministic code, never a model call.

---

## Agent run state machine

States: `queued → running → (awaiting_approval) → succeeded | failed | killed`.

- FSM is DB-backed: `agent_runs.status` + transition rows in `events`. Survives restart.
- Executor loop is **bounded**: stops at `max_turns`, `max_tokens`, `cost_ceiling_usd`, or
  timeout. Hitting a cap → transition to `failed` with reason (Phase 3 upgrades this to
  `escalate` instead of fail).
- Each turn writes an Event (model call in/out, tool call in/out) with cost + latency + trace_id.
- Kill: setting a run's `kill_requested` flag is checked between turns → `killed`.

**Trivial demo run:** actor with EchoProvider, one builtin read tool (`get_time` or
`echo`), `max_turns=2`. `POST /runs` → executes synchronously → returns run id. `GET
/runs/{id}/trace` → full ordered Event list with per-step + total cost.

---

## Non-negotiables honored in Phase 0

- **Fail closed:** provider error / schema-validation failure / cost-cap hit → stop, mark run
  failed with reason. No silent weaker path.
- **Bounded model calls:** max turns/tokens/cost/timeout on every run; killable mid-flight.
- **Deterministic where possible:** cost math, tenancy filter, tool-grant check = code.
- **Tenant isolation in shared query layer**, with a cross-tenant-read test asserting failure.
- **Tool registry:** agents get tools only via grants; no direct import.

## Test set (the check ponytail requires)

- `test_tenancy.py` — cross-tenant read returns empty / raises.
- `test_events_replay.py` — run produces events; replaying reconstructs final run state.
- `test_cost.py` — token counts → expected USD; cap enforcement stops the run.
- `test_run_lifecycle.py` — integration: POST /runs (echo) → succeeded → trace has ordered
  events with total cost = sum of step costs.

---

## Out of scope for Phase 0 (named so it's not silently dropped)

Projects/Tasks/DAG, the Lead, departments, Playbooks, policy engine, approvals, memory,
CRM, voice, frontend, Celery/Redis, WebSockets. Each has a later phase.

## Deliverable at the gate

`POST /orgs` (seed) → `POST /runs` (trivial echo agent) → `GET /runs/{id}/trace` showing a
complete, replayable, costed trace. Green test suite. One `README` with run instructions.
