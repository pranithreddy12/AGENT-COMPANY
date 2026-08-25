# Company OS — State & Handover

**One line:** a multi-tenant platform where an AI-staffed company (named agents across 6 departments,
coordinated by a Lead, reviewed by a Critic, gated by governance) turns a goal into a task DAG,
executes it, and produces reviewed deliverables — plus one hardened revenue path that turns a warm
LeadForge lead into a signed proposal, and a real team chat where @mentioning an agent assigns it work.

- **Repo:** https://github.com/pranithreddy12/AGENT-COMPANY — branch `main`, HEAD `1ba412c`, pushed
- **Tests:** 130 passing (`pytest -q`), 26 test files
- **Stack:** FastAPI + SQLAlchemy 2.0 + SQLite (WAL) + Pydantic v2; single-file vanilla HTML/JS console. Python 3.11, no Node build, no frontend framework.

---

## 1. Honest assessment — read this first

Company OS is **a well-built simulation of a company with two genuinely real capabilities bolted on: a revenue path, and (as of this session) real tool use.**

**What is real:**
- **The proposal path** — a warm lead becomes a real, human-gated, client-signable document. The only part that directly touches revenue. See §5.
- **The engine** — critical-path DAG scheduling, governance (kill switch, budgets, approval gates), multi-tenancy, provider abstraction, audit log. Real code, real tests.
- **Team chat** — @mentioning an agent creates real work (a Task, or for the Lead, a real drafted Project), not a chat reply. See §6.
- **Tool use** — as of this session, agents on Mistral/OpenRouter/Ollama can genuinely call tools mid-task (previously silently broken — see §4). `web_search` is wired to Serper and granted broadly.
- **Per-agent + org-level configuration** — edit any agent's prompt/autonomy/budget from the console; set the org's LLM provider/model/key from one panel, applied instantly to every agent, no restart. See §4 and the Agents/Governance tabs.

**What is not:**
- **Agents describe work, they mostly don't do it.** "Dev Agent" still writes a document *about* code, not code. Nothing is built, deployed, or executed except the LeadForge proposal send and (new) live web searches.
- **Nothing is autonomous end to end.** Every outward action is human-gated by design. Correct and safe, but the opposite of "runs the company without you."
- **Output validation is still prompt-only.** The proposal generator has produced invented prices and placeholder text in the past (§7) — nothing downstream catches that deterministically yet.

The framing "AI-staffed company" is still mostly the demo. **The proposal loop is the product; team chat + real tool use are what make the rest of the app worth using day to day.**

---

## 2. How to run it

```bash
pip install -r requirements.txt
# keys go in .env (gitignored, NOT .env.example): MISTRAL_API_KEY=... SERPER_API_KEY=... OPENROUTER_API_KEY=...
C:/Users/prani/AppData/Local/Programs/Python/Python311/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

**Use the explicit Python 3.11 path above.** The shell's default `python` can resolve to a bare
Python 3.14 install with none of this project's dependencies — bit this repeatedly across sessions.

**Ports 8000 and 8001 are occupied on this machine** by other local projects ("AI Business Operating
System" and LeadForge AI). Company OS runs on **8010**. `startup.bat` still hardcodes 8000 and will
fail to bind — unfixed, see §7.

- Console: http://127.0.0.1:8010/console — sign in with **email + password** (no token pasting)
- API docs: http://127.0.0.1:8010/docs
- Demo login: `launch@test.com` / `test123` (org "Launch Co")

**Restart discipline:** `app/static/console.html` is served fresh on every request — pure CSS/JS
edits need **no restart**. Any `.py` backend change needs a full restart (kill the uvicorn process,
confirm the PID is actually this project's Python before killing it, relaunch).

Providers currently configured in `.env`: **MISTRAL=set, SERPER=not set, OPENROUTER=not set (but
configurable per-org from the console — see §4), ANTHROPIC=not set.**

---

## 3. Architecture — three strictly separate layers

1. **Work layer** — Projects, Tasks (DAG with deps), Artifacts, deterministic critical-path scheduler. LLM-agnostic.
2. **Worker layer** — `Actor` = agent or human. Agents run bounded executions; humans get the same primitives.
3. **Governance layer** — policy engine, autonomy levels, approval queue, budgets, kill switch, simulation. Nothing reaches a client without passing it.

### The agent roster (per org, seeded on `POST /orgs`)

| Agent | Role | Tools | Notes |
|---|---|---|---|
| **Cora** Lead Agent | decomposes goals into a task DAG, routes to departments, never does the work | none | @mentioning her in team chat now correctly calls `planning.draft_project` (see §6) |
| **Piper** Planning | schedules, critical path, retros | echo, get_time, **web_search** | |
| **Sam** Sales | pipeline, proposals, pricing | echo, get_time, **web_search** | |
| **Mia** Marketing | positioning, content, copy | echo, get_time, **web_search** | |
| **Devin** + **Dana** Development | scoping, architecture, specs, QA | echo, get_time, **web_search** | |
| **Lena** Legal | clause review, risk flags — **has veto** | echo, get_time, **web_search** | |
| **Cleo** Client Management | client relationship, status, scope-change detection | echo, get_time, **web_search** | |
| **Quinn** QA Agent | Critic — reviews every artifact, rejects placeholders, capped revise loop | none | |
| **Rex** Research Agent | runs first, web search → sourced brief → shared memory | `web_search` | now actually works (was dangling before this session — registered but not implemented) |

Every agent's `system_prompt`, `autonomy_default`, `max_turns`, `max_tokens`, `cost_ceiling_usd` is
now editable individually from the **Agents** tab (`PATCH /agents/{id}`, ceo/dept_head). Provider/model
are deliberately **not** editable per-agent there — that's owned by the org-wide **Model** panel in
**Governance** (`POST /settings/llm`), which re-points every agent at once; letting both edit the same
thing would mean one silently clobbers the other.

### How agents coordinate
- **Shared project memory** (`MemoryRecord`) — each agent writes what it produced; every agent reads upstream deliverables before working (`planning._gather_context`), so output builds on the team's work, not a blank slate.
- **Team chat** — see §6. The main way to assign work day to day.
- **Ask-an-agent** — `POST /agents/{id}/ask`, first person, grounded in scorecard + deliverables. Still a 512-token status Q&A endpoint, not a work endpoint — use team chat for actual work.
- **Agents tab work history** — `GET /agents` now returns each agent's real task history bucketed into `completed` / `in_progress` / `scheduled` / `blocked`, each entry carrying its project and a real timestamp (artifact creation time, or scheduled start), not fabricated.

---

## 4. Tool use — fixed this session, previously silently broken

**The bug:** `OllamaProvider.complete()` (inherited by `MistralProvider` **and** `OpenRouterProvider` —
i.e. every provider this org has actually used) completely ignored the `tools` parameter and always
returned `tool_calls=[]`, `stop_reason="end"`. Only `AnthropicProvider` (never configured with a key
here) implemented real tool-calling. The multi-turn tool-execution loop in `runs.py` was always real
and already worked — but no OpenAI-compatible provider ever told the model a tool existed, so a
granted tool could never be invoked regardless of `tool_grants`. This is why Rex's `web_search` grant
did nothing even when it existed.

**Fixed:** `OllamaProvider._chat()` now translates our internal tool shape into OpenAI's
function-calling format, sends it, and parses `message.tool_calls` back into real `ToolCall`s
(malformed JSON args degrade to `{}`, never a crash). `complete()` reports `stop_reason="tool_use"`
when the model actually calls something.

**`web_search`** ([`app/services/tools.py`](app/services/tools.py)) is now a real tool: registered in
`BUILTINS`, wired to the already-working Serper integration (`research.serper_search`), capped at 10
results. New orgs get it registered + granted to every worker agent at creation. Existing orgs get an
idempotent startup backfill (`main.backfill_web_search`, runs every launch, safe to run twice).

**Still needed:** `SERPER_API_KEY` is not set in `.env`. The plumbing is fully correct and proven live
(see verification note below) — without a real key, any tool call fails closed with
`"tool web_search: SERPER_API_KEY not set"`, which is honest and correct behavior, just not useful
yet. Get a free key at serper.dev and add it to unlock real search results.

**Live proof this actually works (not just green tests):** mentioning `@sam` with a request that
should trigger a search produced exactly `"tool web_search: SERPER_API_KEY not set"` — a message
only reachable if the tool was genuinely offered to the model, the model genuinely decided to call
it, and the execution loop genuinely dispatched and failed closed on the missing key. Before the fix,
the same request would have silently produced a guessed answer with zero search attempt.

**Adding a new tool** is still cheap: a dict entry in `BUILTINS` (fn + side_effect + input_schema +
description) + a grant in `tool_grants`. The architecture was always fine; the provider layer wasn't.

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

`GET /teamchat` · `POST /teamchat` · console sidebar → **Team chat** (full-screen 3-column messenger:
agent roster rail / message thread / info panel — all real data, no fabricated channels/files/presence)

@mentioning an agent creates **real work**, not a talk reply:
- **Any regular agent** gets a real Task, run through the same machinery as any other work (department agent → Critic → Legal veto → Artifact), then posts its result back into the thread.
- **The Lead (Cora)** is special-cased: mentioning her calls `planning.draft_project` directly — the exact function the Dashboard's "Plan it" button calls — instead of the generic single-task executor. This was a real bug fix this session: routing her through the generic executor (which is what regular agents use) made her respond like a generic chatbot ("Hi, tell me your goal") instead of actually decomposing anything, because her real decomposition logic lives in `provider.plan()`, not in the generic completion path her `AgentProfile.system_prompt` feeds. Fixed the routing, not the prompt.

```
@cleo draft a proposal for the BizBuySell scrape
@cora launch a customer referral program        <- drafts a REAL project with real tasks
```

Roster handles are agent first names: `@cora @piper @sam @mia @devin @dana @lena @cleo @quinn @rex`

- Returns instantly; each mention's work runs in a background thread (never blocks the UI on a model call).
- Multiple mentions → one job each, deduped. No mention → plain chatter, no work created.
- A Legal-blocked artifact reports the veto and **withholds its text**.
- Failures reply in-chat with the reason, so nobody waits forever.
- Agent replies render as real markdown (headings/bold/lists/code — a small safe non-exhaustive renderer, escapes first, only ever wraps in a fixed known tag set) and long replies collapse with a "Show more" toggle instead of dumping walls of text. Backend reply cap raised from a too-aggressive 1500 chars to 8000.
- Reuses `Thread`/`Message`; the team thread carries an effectively unbounded message budget so a human chat can't trip the agent-loop escalation guard that fires at 6 messages.

---

## 7. Known defects — found by running real work, not yet fixed

**Output quality (prompt-only, no deterministic validator yet):**
1. **Invented prices seen historically.** `_PROPOSAL_SYSTEM` permits only `[price to confirm]`, but the model has produced real-looking numbers anyway. A quote nobody approved, one click from a client.
2. **Placeholder signature blocks seen historically** — `[Your Name]`, `[Your Agency Name]`. The prompt says never use placeholders; not deterministically enforced.

→ Fix with a **deterministic post-generation validator** (reject unapproved numbers + `[...]` tokens), not another LLM. Still not built.

**Governance:**
3. **Legal is a coarse keyword screen**, not a real compliance review — documented honestly in the code, but worth remembering before trusting it on anything sensitive (e.g. a proposal whose content implies ToS-violating methods).

**Ops:**
4. `startup.bat` hardcodes port 8000, permanently occupied on this machine → fails to bind. Should auto-pick a free port. Still unfixed.
5. Multiple demo orgs accumulate in `company_os.db` (each `POST /orgs` seeds a full roster). Harmless, but noisy.
6. `SERPER_API_KEY` not set — see §4.

---

## 8. The plan — prioritized

Ordered by distance from revenue.

### Now (small, high leverage)
1. **Add `SERPER_API_KEY` to `.env`** (2 min, get one at serper.dev). Unlocks real web search results — the plumbing is done, this is the only missing piece.
2. **Output validator** (~30 min, code not agent). Reject unapproved numbers and `[...]` placeholders post-generation on proposals. Kills defects 1-2 in §7.
3. **Fix `startup.bat` port** (~10 min).

### Next (closes the revenue loop)
4. **Email send** (Resend/Postmark, ~1h, `side_effect: irreversible` + approval gate). Today Company OS drafts and LeadForge sends; this closes the loop in one system.
5. **Stripe invoice on accept** (~1-2h). The accept flow currently ends at `accepted` and then nothing. An invoice on signature is the difference between a signature and money.
6. **Calendar** (Cal.com, ~1h). The real next step after a proposal is a booked call.
7. **Onboarding flow** — after accept, collect credentials/access, run a kickoff checklist. Currently manual on every deal.

### Later (the bigger bets)
8. **Code execution sandbox** (E2B / Modal / Vercel Sandbox). Makes Dev agents real — they ship code instead of describing it. Longest payback; this is the "autonomous company" bet, not the revenue bet.
9. **Verifier / Fact-Checker agent** — reviews drafts against sources before the Critic, now genuinely possible with real web_search.
10. **Compliance agent** — reads ToS/regulatory context per deal; the judgment a keyword list can't make.
11. **Postgres + Alembic** — required before multi-user. Schema changes currently go through `db._migrate_sqlite`.
12. **More tools beyond web_search** — the pattern is proven and cheap now (dict entry + grant): calendar read/write, CRM lookups, file/doc generation.

### Always
- **Run one real deal.** The machine works; conversion is unmeasured. No amount of additional code substitutes for one real client accepting one real proposal.

---

## 9. Open risks

- **Mistral key** that briefly appeared in a tracked file earlier in this project's history — rotate if not already done.
- **Concurrency ceiling.** Background work is raw daemon threads on one SQLite file. Fine at this scale; under real concurrency a proposal/tool call holds the write lock through its LLM call. Deferred fix: DB-backed job row + single worker, if `database is locked` ever appears.
- **OpenRouter cost accounting is $0 by design, not by accident.** OpenRouter's model catalog is open-ended (any string), so it can never be fully covered by the static `cost.RATES` table. `build_provider` registers any OpenRouter model at $0 via `cost.register_free()` so a successful completion doesn't get discarded as a cost-accounting failure (this was a real live bug, fixed this session) — but that means **budget caps do not currently protect OpenRouter spend**. Worth wiring real per-model OpenRouter pricing (their `/models` endpoint returns it) before relying on cost ceilings with that provider.

---

## 10. Principles worth keeping

- **Reach for code, not an agent, when the rule is expressible.** "No invented prices" is a validator, not a personality. Use agents for judgment over open-ended input (compliance reading, verification against sources).
- **The gate that matters is human approval.** Legal's keyword screen is a coarse first pass and is documented as such. Don't let it grow into false confidence.
- **Ship the path a client walks.** Features on the proposal → signature → payment path earn their keep.
- **Fix the root cause, not the symptom.** Three real examples this session: the Lead "chatbot reply" bug was a routing problem, not a prompt problem; "agents need internet access" was a provider-layer tool-calling bug, not a missing tool; a 500 on `/activity` was a dropped import, caught by adding an HTTP-level smoke test rather than just patching the one line.
- **Verify against the live server, not just green tests.** `pytest` alone missed the OpenRouter URL-doubling bug, the cost-accounting bug that silently discarded successful replies, the Lead-routing bug, and the `/activity` 500 — all only visible by actually hitting the running server. This is now standard practice for every change in this repo.
- **When matching a reference UI/mockup, never fabricate data.** Adapt the structural pattern (layout, visual language) to real backend data; drop or genuinely wire up any mockup element that has no real counterpart, don't fake it client-side.

---

## 11. Key files

| Path | What |
|---|---|
| `app/models.py` | all entities — `Organization` now carries `llm_provider/llm_model/llm_api_keys` (per-provider key map) |
| `app/services/planning.py` | Lead, `draft_project`, `execute_project`, `rerun_task`, memory/chat wiring |
| `app/services/integrations.py` | LeadForge handoff + the whole proposal path |
| `app/services/teamchat.py` | team chat, @mention → real task, Lead → real project |
| `app/services/tools.py` | the tool registry — `web_search` now real (§4) |
| `app/services/llm.py` | provider abstraction (Echo / Ollama / Mistral / OpenRouter / Anthropic) + real tool-calling for OpenAI-compatible providers |
| `app/services/review.py` | Critic + Legal veto |
| `app/services/runs.py` | bounded execution, tool dispatch, cost accounting |
| `app/routers/settings.py` | org-level LLM "brain" config (`GET/POST /settings/llm`) |
| `app/routers/intelligence.py` | agent roster, `PATCH /agents/{id}`, real bucketed work history, hire/retro |
| `app/static/console.html` | the entire UI — dark-sidebar/light-content theme, full-screen team chat, Agents tab with inline edit + history |
| `scripts/run_one_deal.py` | live end-to-end proposal rehearsal |
| `tests/test_console_endpoints_smoke.py` | HTTP-level smoke test over every console GET endpoint — add new console-facing endpoints here |
| `docs/STATUS.md` | **stale, superseded by this file** — do not trust it |
