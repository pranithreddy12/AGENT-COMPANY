# Company OS — State & Plan

**One line:** a multi-tenant platform where an AI-staffed company (named agents across 6 departments,
coordinated by a Lead, reviewed by a Critic, gated by governance) turns a goal into a task DAG,
executes it, and produces reviewed deliverables — plus one hardened revenue path that turns a warm
LeadForge lead into a signed proposal.

- **Repo:** https://github.com/pranithreddy12/AGENT-COMPANY — branch `main`, HEAD `7c5e0d8`, pushed
- **Tests:** 92 passing (`pytest -q`), 21 test files
- **Stack:** FastAPI + SQLAlchemy 2.0 + SQLite (WAL) + Pydantic v2; single-file vanilla HTML/JS console. Python 3.11, no Node build.

---

## 1. Honest assessment — read this first

Company OS is **a well-built simulation of a company with one genuinely useful real path bolted on.**

**What is real:**
- The **proposal path** — a warm lead becomes a real, human-gated, client-signable document. This is the only part that touches revenue.
- The **engine** — critical-path DAG scheduling, governance (kill switch, budgets, approval gates), multi-tenancy, provider abstraction, audit log. Real code, real tests.

**What is not:**
- **Agents describe work, they don't do it.** "Dev Agent" writes a document *about* code. Nothing is built, deployed, sent, or executed.
- **The tool registry is empty.** See §4 — this is the single biggest constraint on the whole system.
- **Nothing is autonomous end to end.** Every outward action is human-gated by design. Correct and safe, but the opposite of "runs the company without you."

The framing "AI-staffed company" is the demo. **The proposal loop is the product.**

---

## 2. How to run it

```bash
pip install -r requirements.txt
# keys go in .env (gitignored, NOT .env.example): MISTRAL_API_KEY=... SERPER_API_KEY=...
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

**Ports 8000 and 8001 are occupied on this machine** by other projects ("AI Business Operating
System" and LeadForge AI). Company OS runs on **8010**. `startup.bat` still hardcodes 8000 and will
fail to bind — see §7.

- Console: http://127.0.0.1:8010/console — sign in with **email + password** (no token pasting)
- API docs: http://127.0.0.1:8010/docs
- Demo login: `launch@test.com` / `test123` (org "Launch Co", on `mistral-small-latest`)

Providers: **MISTRAL=set, SERPER=not set, ANTHROPIC=not set.**

---

## 3. Architecture — three strictly separate layers

1. **Work layer** — Projects, Tasks (DAG with deps), Artifacts, deterministic critical-path scheduler. LLM-agnostic.
2. **Worker layer** — `Actor` = agent or human. Agents run bounded executions; humans get the same primitives.
3. **Governance layer** — policy engine, autonomy levels, approval queue, budgets, kill switch, simulation. Nothing reaches a client without passing it.

### The agent roster (per org, seeded on `POST /orgs`)

| Agent | Role | Tools |
|---|---|---|
| **Cora** Lead Agent | decomposes goals into a task DAG, routes to departments, never does the work | none |
| **Piper** Planning | schedules, critical path, retros | echo, get_time |
| **Sam** Sales | pipeline, proposals, pricing | echo, get_time |
| **Mia** Marketing | positioning, content, copy | echo, get_time |
| **Devin** + **Dana** Development | scoping, architecture, specs, QA | echo, get_time |
| **Lena** Legal | clause review, risk flags — **has veto** | echo, get_time |
| **Cleo** Client Management | client relationship, status, scope-change detection | echo, get_time |
| **Quinn** QA Agent | Critic — reviews every artifact, rejects placeholders, capped revise loop | none |
| **Rex** Research Agent | runs first, web search → sourced brief → shared memory | `web_search` (**broken, see §4**) |

### How agents coordinate
- **Shared project memory** (`MemoryRecord`) — each agent writes what it produced; every agent reads upstream deliverables before working (`planning._gather_context`), so output builds on the team's work, not a blank slate.
- **Team chat** — see §6.
- **Ask-an-agent** — `POST /agents/{id}/ask`, first person, grounded in scorecard + deliverables. **This is a 512-token status endpoint, not a work endpoint** (see §7).

---

## 4. THE core constraint: the tool registry is empty

The entire toolbox ([`app/services/tools.py`](app/services/tools.py)):

| Tool | What it does |
|---|---|
| `echo` | returns the text passed in |
| `get_time` | returns the clock |

**Every deliverable is a model writing prose from its own weights, with zero contact with reality.**
This is the root cause of the invented facts in §7.

Two specific defects:
- **`web_search` is granted but not implemented.** `orgs.py:118` grants it to Rex; it's absent from `BUILTINS`, so calling it raises `ToolError`. **Rex cannot research.**
- **Real web search exists but isn't a tool.** `research.serper_search` works, but it's hardcoded into the proposal path only (`integrations.py:226`). No agent can reach it.
- **Every hire gets toy tools.** `intelligence.py:69` hardcodes `tool_grants=["echo","get_time"]`, so `/hire` produces another prose generator by construction.

**The plumbing is done.** `_run_and_review` → `runs.execute` already passes `tools=tool_specs` from
profile grants (`runs.py:84`) and enforces per-tool grants, side-effect classes (`read|write|irreversible`),
bounded turns, cost accounting, and audit. **Adding a real integration is a dict entry + a grant.**
You are not missing architecture. You are missing tools.

---

## 5. The proposal path — the product

LeadForge posts a warm prospect → Company OS drafts one client-ready proposal → a human approves →
the client signs. Async, idempotent, and gated at every step.

```
LeadForge ─POST /proposal─► generating ─(background)─► ready
                                                        │ human approves in console
                                                        ▼ (mints share link)
                          approve returns share_url ──► send prospect /p/{token}
                                                        │ client reads + clicks Accept (types name)
                                                        ▼
                                                    accepted ── immutable signature row
                                                        │
LeadForge ─GET /proposals/{id}─► {accepted, accepted_by, accepted_at}   ← conversion signal
```

- `POST /integrations/leadforge/proposal` → `{proposal_id, status:"generating"}` **instantly, no draft text**. Research + LLM + Legal run in a background thread.
- **Idempotent** on `leadforge_lead_id` — unique index `(org_id, leadforge_lead_id)` + early-return, so a webhook retry returns the same proposal, never a duplicate.
- `GET /proposals/{id}` → status only until approved; **releases text only when approved and not blocked** — the real send gate.
- `POST /proposals/{id}/approve` (ceo/dept_head only, no webhook-secret path — a machine can't self-approve). Mints the client share token.
- `GET /p/{token}` → public client page (proposal + accept form). `POST /p/{token}/accept` records name + time + IP + SHA-256 of the exact text signed. Idempotent; one signature per proposal.
- Self-recovering: failed generation → `failed` with dedup slot freed; `main.recover_stuck` resets stuck `generating` on restart.
- **Legal is a coarse keyword screen** (`review.PROHIBITED_MARKERS`), not a compliance review. Human approval is the real gate.

**Rehearsal:** `python -m scripts.run_one_deal` drives the whole contract against a live server.

---

## 6. Team chat — assign work by @mentioning agents

`GET /teamchat` · `POST /teamchat` · console sidebar → **Team chat**

@mentioning an agent creates a **real Task** assigned to it and runs it through the same machinery as
any other work (department agent → Critic → Legal veto → Artifact), then the agent posts its result
back into the thread. The chat is a task queue with governance intact, not a talk shop.

```
@cleo draft a proposal for the BizBuySell scrape
```

Roster handles are agent first names: `@cora @piper @sam @mia @devin @dana @lena @cleo @quinn @rex`

- Returns instantly; each task runs in a background thread (never blocks the UI on a model call).
- Multiple mentions → one task each, deduped. No mention → plain chatter, no work created.
- A Legal-blocked artifact reports the veto and **withholds its text**.
- Failures reply in-chat and mark the task blocked, so nobody waits forever.
- Reuses `Thread`/`Message`; the team thread carries an effectively unbounded message budget so a human chat can't trip the agent-loop escalation guard that fires at 6 messages.

---

## 7. Known defects — found by running real work

These are real, reproduced, and unfixed.

**Output quality (root cause: §4 — no tools, nothing to verify against):**
1. **Invented prices.** The BizBuySell proposal produced `[$12,500]`. `_PROPOSAL_SYSTEM` permits only `[price to confirm]`. **A quote nobody approved, one click from a client.**
2. **Placeholder signature block** — `[Your Name]`, `[Your Agency Name]`, `[Your Email]`. The prompt says *never* use placeholders.
3. **Invented technical facts.** An agent recommended `text-davinci-003`, retired by OpenAI in January 2024.

→ Fix with a **deterministic post-generation validator** (reject unapproved numbers + `[...]` tokens), not another LLM.

**Wrong-surface confusion:**
4. `POST /agents/{id}/ask` is a **512-token status Q&A endpoint** whose prompt says "answer the human directly, in first person." Asking it for a proposal returns a chat reply *about* a proposal — meta-commentary, invented prices, no Legal review, no approval gate. Team chat (§6) now routes real requests to the real pipeline; consider making `/ask` refuse work-shaped requests.

**Governance:**
5. **Legal passed a proposal advertising anti-bot evasion.** The BizBuySell draft offered "proxy setup" / "rotation/headers to avoid blocks" — written intent to evade, in a document you'd sign. The keyword screen structurally cannot catch this. See §9.

**Ops:**
6. `startup.bat` hardcodes port 8000, permanently occupied on this machine → fails to bind. Should auto-pick a free port.
7. Multiple demo orgs accumulate in `company_os.db` (each `POST /orgs` seeds a full roster). Harmless, but noisy.

---

## 8. The plan — prioritized

Ordered by distance from revenue. **Do not add agents before steps 1-2** — see §10.

### Now (small, high leverage)
1. **Register Serper as a real tool** (~30 min). It already exists. Add to `BUILTINS`, grant to all agents, fix Rex's dangling grant. Biggest quality jump per unit effort: agents stop hallucinating because they can look things up.
2. **Output validator** (~30 min, code not agent). Reject unapproved numbers and `[...]` placeholders post-generation. Kills defects 1-2 above.
3. **Fix `startup.bat` port** (~10 min).

### Next (closes the revenue loop)
4. **Email send** (Resend/Postmark, ~1h, `side_effect: irreversible` + approval gate). Today Company OS drafts and LeadForge sends; this closes the loop in one system.
5. **Stripe invoice on accept** (~1-2h). The accept flow currently ends at `accepted` and then nothing. An invoice on signature is the difference between a signature and money.
6. **Calendar** (Cal.com, ~1h). The real next step after a proposal is a booked call.
7. **Onboarding flow** — after accept, collect credentials/access, run a kickoff checklist. Currently manual on every deal.

### Later (the bigger bets)
8. **Code execution sandbox** (E2B / Modal / Vercel Sandbox). Makes Dev agents real — they ship code instead of describing it. Longest payback; this is the "autonomous company" bet, not the revenue bet.
9. **Verifier / Fact-Checker agent** — reviews drafts against sources before the Critic. Only works after step 1.
10. **Compliance agent** — reads ToS/regulatory context per deal; the judgment a keyword list can't make.
11. **Postgres + Alembic** — required before multi-user. Schema changes currently go through `db._migrate_sqlite`.

### Always
- **Run one real deal.** The machine works; conversion is unmeasured. No amount of additional code substitutes for one real client accepting one real proposal.

---

## 9. Open risks

- **BizBuySell scraping (active lead).** Their ToS almost certainly prohibits automated scraping, and the draft proposal advertises evading anti-bot measures. Scraping public listings is common and defensible; **putting evasion in a signed proposal is not** — it's written evidence of intent. Strip the evasion language and resolve the ToS question before signing. Notably, Cleo flagged this unprompted in team chat; the keyword gate did not.
- **Mistral key** that briefly appeared in a tracked file (`ESF4…`) — rotate if not already done.
- **Concurrency ceiling.** Background work is raw daemon threads on one SQLite file. Fine at this scale; under real concurrency a proposal holds the write lock through its LLM call. Deferred fix: DB-backed job row + single worker, if `database is locked` ever appears.

---

## 10. Principles worth keeping

- **More agents ≠ more capability.** All agents share the same two toy tools. A new "Finance Agent" is a new voice writing about invoices, not one sending them. Headcount isn't the bottleneck; the empty `BUILTINS` dict is.
- **Reach for code, not an agent, when the rule is expressible.** "No invented prices" is a validator, not a personality. Half of proposed "agents" are validators wearing a costume. Use agents for judgment over open-ended input (compliance reading, verification against sources).
- **The gate that matters is human approval.** Legal's keyword screen is a coarse first pass and is documented as such. Don't let it grow into false confidence.
- **Ship the path a client walks.** Features on the proposal → signature → payment path earn their keep. Everything else is demo surface.

---

## 11. Key files

| Path | What |
|---|---|
| `app/models.py` | all entities |
| `app/services/planning.py` | Lead, `execute_project`, `rerun_task`, memory/chat wiring |
| `app/services/integrations.py` | LeadForge handoff + the whole proposal path |
| `app/services/teamchat.py` | team chat, @mention → real task |
| `app/services/tools.py` | **the tool registry (§4)** |
| `app/services/llm.py` | provider abstraction (Echo / Ollama / Mistral / Anthropic) |
| `app/services/review.py` | Critic + Legal veto |
| `app/services/runs.py` | bounded execution, tool dispatch, cost accounting |
| `app/static/console.html` | the entire UI |
| `scripts/run_one_deal.py` | live end-to-end proposal rehearsal |
| `docs/STATUS.md` | **stale** (HEAD 2043b63 / 75 tests / port 8000 / token-paste console) — this file supersedes it |
