"""Governance: the deterministic gate between agents and the outside world.

Nothing reaches a client, a payment, or a public channel without passing evaluate(). All checks
are code (kill switch, budget, policy, autonomy) — models draft and judge; they never authorize.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Actor, AgentProfile, AgentRun, ApprovalRequest, Department, Event, Organization, Policy,
)
from app.services import events

# Action types with external effect always need a human (non-negotiable list, abbreviated).
ALWAYS_APPROVAL = {"client_send", "publish", "contract_send", "spend", "delete"}
_AUTONOMY_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}


class Denied(Exception):
    def __init__(self, reason: str, rule: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.rule = rule


@dataclass
class Decision:
    effect: str  # allow | require_approval | deny
    reason: str
    rule: str | None = None


def spent(db: Session, org_id: str) -> float:
    return db.scalar(select(func.coalesce(func.sum(AgentRun.cost_usd), 0.0)).where(AgentRun.org_id == org_id)) or 0.0


def remaining_budget(db: Session, org: Organization) -> float:
    return org.cost_cap_usd - spent(db, org.id)


def _match(condition: dict, action_type: str, content: str) -> bool:
    if "action_type" in condition and condition["action_type"] != action_type:
        return False
    if "contains_any" in condition:
        low = (content or "").lower()
        if not any(w.lower() in low for w in condition["contains_any"]):
            return False
    return True  # empty condition matches everything


def _autonomy_level(profile: AgentProfile | None, action_type: str) -> str:
    if profile is None:
        return "L1"
    return (profile.autonomy_overrides or {}).get(action_type, profile.autonomy_default)


def evaluate(db: Session, org: Organization, actor: Actor | None, dept: Department | None,
             action_type: str, content: str) -> Decision:
    # 1. kill switch (global, then per-department)
    if org.killed:
        return Decision("deny", "global kill switch is active", "kill_switch")
    if dept and dept.paused:
        return Decision("deny", f"department {dept.name} is paused", "dept_pause")

    # 2. budget hard cap -> escalate, never silently truncate
    if remaining_budget(db, org) <= 0:
        return Decision("require_approval", "budget cap reached; escalating", "budget_cap")

    # 3. policy engine: highest priority matching rule wins; denials are logged with the rule
    policies = db.scalars(
        select(Policy).where(Policy.org_id == org.id, Policy.active == True)  # noqa: E712
        .order_by(Policy.priority.desc())
    )
    policy_effect = None
    for p in policies:
        if _match(p.condition, action_type, content):
            policy_effect = Decision(p.effect, f"policy '{p.name}'", p.name)
            break

    if policy_effect and policy_effect.effect == "deny":
        return policy_effect

    # 4. non-negotiable + autonomy
    profile = db.get(AgentProfile, actor.agent_profile_id) if actor and actor.agent_profile_id else None
    if action_type in ALWAYS_APPROVAL:
        return Decision("require_approval", "external send always needs human approval", "always_approval")
    if policy_effect and policy_effect.effect == "require_approval":
        return policy_effect
    if _AUTONOMY_RANK[_autonomy_level(profile, action_type)] < _AUTONOMY_RANK["L2"]:
        return Decision("require_approval", f"autonomy below L2 for {action_type}", "autonomy")
    return Decision("allow", "within policy and autonomy", policy_effect.rule if policy_effect else None)


# ---- outbound flow ----

def last_rejection_reason(db: Session, org_id: str, actor_id: str | None, action_type: str) -> str | None:
    ar = db.scalars(
        select(ApprovalRequest).where(
            ApprovalRequest.org_id == org_id, ApprovalRequest.action_type == action_type,
            ApprovalRequest.requested_by_actor_id == actor_id, ApprovalRequest.status == "rejected",
        ).order_by(ApprovalRequest.decided_at.desc())
    ).first()
    return ar.decision_reason if ar else None


def draft_outbound(db: Session, org_id: str, actor_id: str | None, action_type: str, intent: str) -> str:
    """Agent drafts the outbound content, folding in the latest CEO rejection reason so a retry
    visibly differs from the rejected attempt."""
    content = f"To client: {intent}"
    reason = last_rejection_reason(db, org_id, actor_id, action_type)
    if reason:
        content += f" | revised per CEO feedback: {reason}"
    return content


def _perform_send(db: Session, org: Organization, actor_id: str | None, action_type: str, content: str) -> Event:
    # tool boundary: in simulation nothing leaves the building; the event is tagged.
    return events.append(
        db, org_id=org.id, trace_id="outbound", actor_id=actor_id,
        action=f"{action_type}.sent", target="client", after={"content": content},
        simulated=org.simulation,
    )


def request_outbound(db: Session, org: Organization, actor: Actor, action_type: str, intent: str) -> dict:
    dept = db.get(Department, actor.department_id) if actor.department_id else None
    content = draft_outbound(db, org.id, actor.id, action_type, intent)
    decision = evaluate(db, org, actor, dept, action_type, content)

    events.append(db, org_id=org.id, trace_id="outbound", actor_id=actor.id,
                  action=f"{action_type}.evaluated", after={"effect": decision.effect, "rule": decision.rule})

    if decision.effect == "deny":
        db.commit()
        raise Denied(decision.reason, decision.rule)

    if decision.effect == "require_approval":
        ar = ApprovalRequest(
            org_id=org.id, action_type=action_type, payload={"intent": intent, "content": content},
            preview=content, requested_by_actor_id=actor.id, department_id=actor.department_id,
            status="pending", expires_at=datetime.now(timezone.utc) + timedelta(days=2),
        )
        db.add(ar)
        db.commit()
        return {"status": "pending_approval", "approval_id": ar.id, "preview": content, "rule": decision.rule}

    ev = _perform_send(db, org, actor.id, action_type, content)
    db.commit()
    return {"status": "sent", "event_id": ev.id, "content": content, "simulated": ev.simulated}


def decide(db: Session, org: Organization, approval: ApprovalRequest, approver_user_id: str,
           decision: str, reason: str | None) -> dict:
    if approval.status != "pending":
        raise Denied(f"approval already {approval.status}")
    approval.approver_user_id = approver_user_id
    approval.decision_reason = reason
    approval.decided_at = datetime.now(timezone.utc)
    if decision == "approve":
        approval.status = "approved"
        ev = _perform_send(db, org, approval.requested_by_actor_id, approval.action_type, approval.preview)
        db.commit()
        return {"status": "approved", "event_id": ev.id, "simulated": ev.simulated}
    approval.status = "rejected"
    db.commit()
    return {"status": "rejected", "reason": reason}
