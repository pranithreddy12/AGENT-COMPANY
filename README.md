# Company OS

An AI-staffed company platform: a task graph with deadlines where the workers are models,
governed and audited end to end. Built in phases (see [docs/phase-0-plan.md](docs/phase-0-plan.md)).

**Status: complete — all 7 phases built, tested (43 passing), and demoed live.**

| Phase | What | Plan |
|---|---|---|
| 0 | Foundations: multi-tenant schema, JWT auth, append-only event log, tool registry, provider-abstracted LLM, bounded run state machine, cost accounting | [plan](docs/phase-0-plan.md) |
| 1 | Work layer + the Lead: Projects/Tasks/DAG, deterministic critical-path scheduler | [plan](docs/phase-1-plan.md) |
| 2 | Full org + communication: 6 departments, Playbooks, HandoffPackets, thread budgets, Critic, Legal veto | [plan](docs/phase-2-plan.md) |
| 3 | Governance: policy engine, autonomy levels, approval queue, budget caps, kill switch, simulation | [plan](docs/phase-3-plan.md) |
| 4 | Human layer: human Actors, paired tasks, review→Playbook amendment, CEO console | [plan](docs/phase-4-plan.md) |
| 5 | Client-facing: CRM, evidence-cited Lead Qualification, client portal, scope-change detection | [plan](docs/phase-5-plan.md) |
| 6 | Voice: transcript→CRM→owned-tasks pipeline behind a provider seam, consent-gated recording | [plan](docs/phase-6-plan.md) |
| 7 | Intelligence & hardening: scorecards, hire-an-agent, retro agent, Playbook A/B, failure injection, Dockerfile | [plan](docs/phase-7-plan.md) |

Run `pytest -q` for the full suite; `uvicorn app.main:app` then open `/console`.

**Phase 5 recap.** A CRM (Accounts, Contacts, Leads) with a
deterministic **Lead Qualification** pipeline — ICP fit + BANT/MEDDIC framework, every claim carrying
**cited evidence**, producing either a **qualified HandoffPacket to Sales** or a **disqualification
with a stated reason** (and an honest "insufficient information" when data is thin, never inventing
fit). Plus a **client portal**: clients `POST /login` and see only their account's projects,
deliverables, and a single message thread — and a request beyond the SOW is detected and routed to
Sales as a **change order**. See [docs/phase-5-plan.md](docs/phase-5-plan.md).

### Phase 1 flow
```bash
# after POST /orgs (from Phase 0), with $TOKEN:
curl -sX POST localhost:8000/projects -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"goal":"Ship a client onboarding flow"}'   # -> reviewable DAG
curl -sX POST localhost:8000/projects/$PID/approve -H "authorization: Bearer $TOKEN"    # schedule + critical path
curl -sX POST localhost:8000/projects/$PID/execute -H "authorization: Bearer $TOKEN"    # artifacts land
curl -sX POST localhost:8000/tasks/$TID/slip -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"added_hours":6}'                            # recompute on slip
```

## Run it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then drive the Phase 0 gate (create a tenant → run the demo agent → read its trace):

```bash
# 1. Bootstrap an org + demo echo agent, capture token + actor id
curl -s -X POST localhost:8000/orgs \
  -H 'content-type: application/json' \
  -d '{"name":"Acme","ceo_email":"ceo@acme.com","ceo_password":"pw"}'

# 2. Trigger a bounded agent run (use actor_id + access_token from step 1)
curl -s -X POST localhost:8000/runs \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"actor_id":"'$ACTOR'","input":"hello"}'

# 3. Read the full replayable trace with per-step + total cost
curl -s localhost:8000/runs/$RUN_ID/trace -H "authorization: Bearer $TOKEN"
```

The demo runs on the zero-cost `EchoProvider` — no API key needed. For a live LLM run, set
`ANTHROPIC_API_KEY`, `pip install anthropic`, and point an AgentProfile at `provider="anthropic"`.

## Test

```bash
pytest -q
```

Covers: tenant isolation (cross-tenant read/write blocked), event replay (log reconstructs run
state), cost math + cap enforcement (fail-closed), full run lifecycle, and the HTTP gate.

## Proving the intelligence (keyed)

Every green test runs on `EchoProvider` — they prove the *machine*, not *agent quality*. The eval
harness ([app/services/evals.py](app/services/evals.py)) runs the real code paths and scores three
things no unit test can: the Lead decomposing a novel goal into a sane DAG, an SOP edit actually
changing agent output (the Playbook goes into the agent's **system prompt**, real in-context loading),
and the Critic passing good work while rejecting empty work. It runs deterministically on Echo (tested)
and against a real model when keyed:

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-...          # Windows: set ANTHROPIC_API_KEY=...
python -m scripts.eval_live              # optionally EVAL_MODEL=claude-opus-4-8
```

The Anthropic plan path validates model JSON with retry-on-invalid ([validate_plan](app/services/llm.py))
and the Critic has an LLM-backed verdict with fail-closed parsing — both unit-tested without a key.

## Layout

See [docs/phase-0-plan.md](docs/phase-0-plan.md). Key rule: three strictly separate layers —
**work** (structural, LLM-agnostic), **worker** (agents + humans as `Actor`s), **governance**
(policy, approvals, audit). Phase 0 builds the worker/audit substrate; later phases add the rest.
