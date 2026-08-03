# Phase 4 — Human layer

**Gate:** a human joins a department, gets assigned real work, reviews an agent's artifact, and
their correction changes future agent behavior **via the Playbook** (not a prompt edit).

## The load-bearing loop (why the Playbook is the control surface)
Agents load their department's **active** Playbook at run time. A Playbook may contain `RULE:` lines
— directives the worker applies to its artifact. So:
1. Execute a task → artifact produced under Playbook v1 (no extra rule).
2. Human reviews the artifact and annotates a correction, which proposes a **Playbook amendment**
   (v2 = v1 + the new `RULE:`).
3. A human activates v2 (supersedes v1).
4. Re-execute → the artifact now reflects the new rule. **Behavior changed via the SOP, prompts untouched.**

## New / changed entities
- Human `Actor` (type=human, linked to a `User`, in a department) — "a human joins a department".
- `Task.reviewer_actor_id` — agent drafts, human reviews (paired task).
- `Playbook` gains `status` (active|draft|superseded) + `change_summary`; versioning by (department, version).
- `annotations` — artifact_id, author, text, proposed_rule, amendment_playbook_id.

## Endpoints
- `POST /team` (ceo) — create a human team member (User + human Actor in a department).
- `POST /tasks/{id}/assign` — set assignee and/or reviewer.
- `POST /artifacts/{id}/annotate` — human review; with `proposed_rule` it drafts a Playbook amendment.
- `POST /playbooks/{id}/activate` — activate a draft amendment (supersede the prior active version).
- `GET /playbooks?department_id=` — version history.
- `GET /console/standup` — the CEO digest (shipped, blocked, at-risk, pending approvals, spend, needs-you).
- `GET /console` — a thin single-screen HTML CEO console over the JSON APIs.

## Tests
- human joins + is assigned + annotates an artifact → amendment → activate → re-run reflects the rule.
- Playbook versioning: only one active per department; activation supersedes the prior.
- standup digest aggregates the right counts.

## Deferred (named)
Next.js/shadcn console (the thin HTML is the lazy stand-in; same JSON APIs). Rich mentoring analytics.
A/B Playbooks and scorecards (Phase 7). Real auth UX / SSO.
