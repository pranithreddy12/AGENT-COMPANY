# Phase 2 — Full org + communication

**Gate:** a project touching four departments completes, with zero unbounded loops, and every
cross-team handoff visible as a structured packet.

## New entities
- `playbooks` — versioned SOP markdown per department (version, effective_from).
- `handoff_packets` — from_dept, to_dept, context, evidence[], open_questions[], confidence.
- `threads` / `messages` — thread_type (request|handoff|escalation|status|client), **message_budget**,
  status (open|resolved|escalated). Posting past budget without resolution auto-escalates.
- `Artifact` gains: blocked, block_reason (Legal veto), needs_human, critic_reasons, reviewed_by.

## Six departments (seeded, each with a charter + Playbook + a worker agent)
Sales · Marketing · Development · Legal · Client Management · Planning. Plus a cross-cutting
**Critic** actor. The Lead's demo DAG spans all six so the gate's "four departments" is easily met.

## Anti-unbounded-loop guarantees (three, all enforced in code + tested)
1. **Bounded runs** (Phase 0): max turns/tokens/cost, killable.
2. **Critic revise cap**: artifact → Critic (pass | revise-with-reasons). Re-run capped at N cycles,
   then escalate to a human (`needs_human`). Never loops forever.
3. **Thread message budget**: `post_message` refuses past budget and flips the thread to `escalated`.

## Structured communication
- Every cross-department dependency edge produces a **HandoffPacket** (upstream artifact = evidence)
  plus a `handoff` thread with one message. This is the "every handoff visible as a packet" property.
- An agent may only *request* work; the Lead/department head converts requests into Tasks. (Modeled:
  agents don't create cross-dept tasks — only the Lead's plan does.)

## Legal veto
A Legal-department task reviews the project's client-facing artifacts and may **block** one
(`artifact.blocked`). No agent can clear it — only a human with role `ceo`/`dept_head` via
`POST /artifacts/{id}/override`. Deterministic demo rule: block when content contains a prohibited
marker; the happy-path gate passes.

## Endpoints
- `GET /projects/{id}/handoffs` — the structured packets.
- `GET /projects/{id}/threads` — threads + messages (shows budgets / escalations).
- `POST /artifacts/{id}/override` — human-only Legal-veto override.

## Tests
- critic: pass path; forced-revise hits the cap and escalates (no infinite loop).
- thread budget: posting past budget escalates.
- legal veto: blocks; agent cannot override, human can.
- multi-dept flow: 6-dept project completes; a HandoffPacket exists for every cross-dept edge.

## Deferred (named)
Client portal, CRM, scope-change detection (Phase 5). Policy engine, approvals, budgets, kill
switch, simulation (Phase 3). Real thread-based multi-agent negotiation (not needed — requests
route through the Lead).
