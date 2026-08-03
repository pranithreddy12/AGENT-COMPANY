# Phase 3 — Governance

**Gate:** an agent attempting a client-facing send is blocked, queued for approval, and the CEO's
rejection reason visibly changes the next attempt.

## New entities
- `policies` — declarative rules: scope/condition (json), effect (allow|require_approval|deny), priority.
- `approval_requests` — action_type, payload preview, requested_by, department, status
  (pending|approved|rejected|expired), decision_reason, approver, expires_at.
- `Organization` gains `killed`, `simulation`. `Department` gains `paused`. `Event` gains `simulated`.
- `AgentProfile` gains `autonomy_overrides` (per-action-type level; falls back to `autonomy_default`).

## Governance service — the gate before every external action
`evaluate(action)` runs deterministic checks in order and returns (effect, rule, reason):
1. **Kill switch** — org `killed` → deny (global halt); department `paused` → deny.
2. **Budget** — spend ≥ cap → require_approval (escalate, never silently truncate).
3. **Policy engine** — rules by priority desc, first match wins; denials logged with the rule that fired.
4. **Autonomy** — external action with autonomy < L2 upgrades `allow` → `require_approval`.
Client sends are always `require_approval` (non-negotiable list), regardless of autonomy.

## Autonomy levels (per agent, per action type)
L0 suggest · L1 draft (needs approval) · L2 act & notify · L3 autonomous. New agents default L1
for external effect.

## Outbound flow (the gate)
- `POST /outbound {actor_id, action_type, intent}` → the agent **drafts** content (folding in the
  latest rejection reason for this actor+action), governance evaluates → `deny` (403 + rule) /
  `pending_approval` (ApprovalRequest queued) / `sent`.
- `GET /approvals` → pending queue with payload preview.
- `POST /approvals/{id}/decide {decision, reason}` (human) → approve performs the send
  (simulation-aware); reject stores the reason, which the **next draft visibly incorporates**.

## Budgets, kill switch, simulation
- Hard cap = `org.cost_cap_usd`; over cap → escalate to approval.
- `POST /governance/kill` · `/resume` · `/departments/{id}/pause` (human-only).
- Simulation mode tags every send `simulated` in the event log and performs no real side effect.

## Tests
- outbound client_send → pending; reject with reason → next draft differs and carries the reason;
  approve → sent.
- forbidden-claim content → deny with the rule id.
- kill switch → outbound denied. budget over cap → escalated. simulated send → event tagged, no effect.

## Deferred (named)
CEO console UI (Phase 4). Real send channels/idempotent outbox (arrives with a real tool in a later
phase). Per-channel rate limits (policy-engine extension, same interface).
