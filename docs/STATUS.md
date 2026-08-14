# Company OS (repo: AGENT-COMPANY) — Project Status

> **STALE — see [`plan.md`](../plan.md) in the repo root for current state.**
> This file describes HEAD `2043b63` / 75 tests / port 8000 / the old token-paste console.
> Since then: client accept + e-sign, team chat (@mention → real task), email/password console
> login, and the tool-registry findings. `plan.md` supersedes this document.

**One line:** a multi-tenant platform where an AI-staffed company (named agents across 6 departments,
coordinated by a Lead, reviewed by a Critic, gated by governance) turns a CEO goal into a task DAG,
executes it, and produces reviewed deliverables — with a web console to run and watch it.

- **Repo:** https://github.com/pranithreddy12/AGENT-COMPANY (branch `main`, HEAD `2043b63`)
- **Tests:** 75 passing (`pytest -q`)
- **Stack:** FastAPI + SQLAlchemy 2.0 + SQLite (WAL) + Pydantic v2; vanilla single-file HTML/JS console.
  No Node build. Python 3.11.

## Architecture (three strictly separate layers)
1. **Work layer** — Projects, Tasks (DAG with deps), Artifacts, a deterministic critical-path scheduler. LLM-agnostic.
2. **Worker layer** — `Actor` = agent or human. Agents run bounded executions; humans get the same primitives.
3. **Governance layer** — policy engine, autonomy levels, approval queue, budgets, kill switch, simulation. Nothing reaches a client/send without passing it.

## The agents (per org, seeded on `POST /orgs`)
- **Cora Lead Agent** (role=lead) — decomposes a goal into a task DAG, routes to departments, never does the work.
- 6 departments each with a named worker: **Sam Sales, Mia Marketing, Devin + Dana Dev, Lena Legal, Cleo Client, Piper Planning**.
- **Quinn QA Agent** (Critic) — reviews every artifact; rejects generic/placeholder output, capped revise loop then escalates.
- **Rex Research Agent** (role=research) — runs FIRST on a project: Serper web search → sourced brief → shared memory. Dormant unless `SERPER_API_KEY` set.

## How agents work together
- **Shared project memory** (`MemoryRecord`): each agent writes what it produced; every agent reads upstream deliverables + memory before working (in `planning._gather_context`), so output is coherent, not blank-slate.
- **Team chat** (`Thread` type=status): first-person messages — Lead kicks off, each agent acknowledges who handed to it and hands to who's next. Bounded (kickoff + start + done per task), commits live so the console streams it.
- **Per-agent memory**: each agent has its own to-do / completed task lists (shown in console, and in its answers).
- **Ask-an-agent**: `POST /agents/{id}/ask` — any agent answers in first person, grounded in its scorecard, deliverables, tasks, and project memory.

## LeadForge proposal path — the "one real deal" flow (hardened, `/plan-eng-review`)
LeadForge posts a warm prospect; Company OS drafts one client-ready proposal, a human approves it, LeadForge sends it. The path is async and safe to run against a real client:
- `POST /integrations/leadforge/proposal` → returns `{proposal_id, status:"generating"}` **instantly, no draft text**. Research + LLM + Legal run in a background thread (`integrations.run_proposal_in_background`).
- **Idempotent** on `leadforge_lead_id`: a unique index `(org_id, leadforge_lead_id)` on `projects` + an early-return check mean a webhook retry returns the same `proposal_id`, never a duplicate proposal. (SQLite migration in `db._migrate_sqlite`, no Alembic yet.)
- `GET /proposals/{id}` → status only until approved; **releases the proposal TEXT only when `status=approved` and not blocked** — the real send gate.
- `POST /proposals/{id}/approve` (ceo/dept_head only, no webhook-secret path) → clears `needs_human`, marks approved. Refuses a still-generating (409) or Legal-blocked (409) proposal.
- Failure/interrupt self-recovers: a failed generation is marked `failed` with its dedup slot freed (retryable); `main.recover_stuck` resets stuck `generating` on restart.
- Legal is a **coarse keyword screen** (`review.PROHIBITED_MARKERS`), not a real compliance review — human approval is the gate. `generate_proposal` remains as a synchronous convenience for direct/test use.

## LLM provider layer (`app/services/llm.py`) — provider-abstracted
- `EchoProvider` (deterministic, zero-cost, default seed + all tests), `OllamaProvider` (local, qwen2.5:7b), `MistralProvider` (cloud, OpenAI-compatible), `AnthropicProvider`.
- Flip an org's agents: `evals.use_mistral(db, org_id, "mistral-small-latest")` / `use_ollama` / `use_anthropic`.
- Plan/critic use JSON with extract-and-retry; planning forces max_tokens ≥ 4096 (Mistral plans are large).

## Current runtime state (as of this writing)
- Server running: `uvicorn app.main:app` at http://127.0.0.1:8000. Console at `/console`, API docs at `/docs`.
- DB: `company_os.db` (SQLite, WAL). Multiple demo orgs exist.
- **Active org "Launch Co"** on `mistral-small-latest`. Login `launch@test.com` / `test123`.
- Providers configured: **MISTRAL=yes, SERPER=no, ANTHROPIC=no** (keys in `.env`, gitignored).
- Latest completed project: "social media content calendar for a coffee subscription brand" — 15/15 tasks done, reviewed.
- Execution is **async**: `POST /projects/{id}/execute` returns instantly (`status: executing`), runs in a background thread, console auto-refreshes. One execution per project (409 guard).

## Run it
```bash
pip install -r requirements.txt
# put keys in .env (NOT .env.example):  MISTRAL_API_KEY=...   SERPER_API_KEY=...
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
Console flow: paste an access token (from `POST /orgs` or `POST /login`) → Dashboard directive "Plan it" → lands on the project → **▶ Run it** → watch Team chat + Deliverables.

## Key files
- `app/models.py` — all entities. `app/routers/*` — HTTP. `app/services/*` — logic:
  `planning.py` (Lead, execute_project, memory/chat/research wiring), `llm.py` (providers), `research.py` (Serper), `talk.py` (ask-agent), `governance.py`, `review.py` (Critic/Legal), `scheduling.py`, `crm.py`, `voice.py`, `evals.py`.
- `app/static/console.html` — the whole UI (classic/elegant theme: ivory, serif headings, navy/gold), 484 lines.

## Known gaps / next steps
- **SERPER_API_KEY not set** → Rex sits out. Add to `.env` to activate web research.
- Output quality is model-bound: Mistral is good; local 7B is generic. Prompts already demand specificity + Critic rejects placeholders.
- Deferred (from code review, low severity): echo-provider guard for Rex, register `web_search` as a real tool, dedupe a double `rex()` query, add org filter in one `talk.py` query.
- Not built for production: still SQLite (Postgres via `DATABASE_URL`), no Alembic (schema changes go through `db._migrate_sqlite`). Company OS drafts + gates proposals but does NOT send — LeadForge fetches approved text via `GET /proposals/{id}` and sends through its own hardened channels.
- Background work is raw daemon threads on one SQLite file (fine at this scale). Under true concurrency a proposal holds the write lock through its LLM call; the deferred move is a DB-backed job row + single worker if `database is locked` ever shows up.

## Reminder
Rotate the Mistral key that briefly appeared in a tracked file earlier (`ESF4…`), if not already done.
