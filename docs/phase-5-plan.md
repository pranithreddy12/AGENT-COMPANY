# Phase 5 — Client-facing

**Gate:** a raw lead becomes a qualified HandoffPacket **with citations**, or a disqualification
**with a stated reason** — and a client can log in and see their project.

## CRM entities
- `accounts` (name, domain, industry, size, is_client) · `contacts` (account, name, email, title)
- `leads` (source, company, industry, size, attributes{budget/authority/need/timeline/notes},
  icp_fit_score, qualification_state, disqualify_reason, confidence, framework, evidence[], handoff_packet_id)
- `change_orders` (project, account, description, status, handoff_packet_id)
- `Project.account_id`, `User.account_id`, `Organization.qualification_framework` (BANT default; MEDDIC configurable).

## Lead Qualification (deterministic — scoring/routing is code, not a model call)
`qualify(lead)`:
1. **ICP fit** — industry in ICP set (+50) + size in range (+50) → 0–100.
2. **Framework** (BANT/MEDDIC) — each dimension → present/absent with **cited evidence**
   (`{claim, evidence, source: "lead.attributes.<key>"}`). Missing fields are cited as gaps, never invented.
3. **Decision** — ICP below threshold → **disqualified (reason)**; too few dimensions →
   **insufficient_info** ("insufficient information: <missing>"); otherwise **qualified** → a
   **HandoffPacket to Sales** carrying the evidence, with a confidence score.
   The agent can say "insufficient information" rather than inventing fit.

## Client portal (role=client, scoped to the client's account)
- `POST /login` — email/password → token (works for CEO, team, and client users).
- `GET /portal/projects` · `GET /portal/projects/{id}` — status/health, deliverables awaiting input,
  the single client thread.
- `POST /portal/projects/{id}/messages` — client posts on the client thread.
- `POST /portal/projects/{id}/requests` — **scope-change detection**: a request beyond the SOW is
  flagged and routed to Sales as a **change order** (HandoffPacket), not silently absorbed.

## Endpoints (CRM)
`POST /leads` · `POST /leads/{id}/qualify` · `GET /leads/{id}` · `POST /clients` (account + client user).

## Tests
- qualify a strong lead → qualified + HandoffPacket + per-claim citations.
- qualify a poor-ICP lead → disqualified with reason. qualify a sparse lead → insufficient_info.
- client logs in → sees only their account's project. scope-change request → change order to Sales.

## Deferred (named)
Real enrichment providers (Clay/Apollo) behind the same `enrich()` seam. Portal file downloads /
approvals UI polish. Voice (Phase 6). Scorecards/hire-an-agent/retro (Phase 7).
