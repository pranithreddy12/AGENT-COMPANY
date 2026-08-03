"""Phase 0 schema subset. Every row carries org_id (tenant isolation).

Only the tables the Phase 0 gate needs. The other 13 entities from the brief land
in the phases that use them.
"""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uuid() -> str:
    return uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String)
    plan: Mapped[str] = mapped_column(String, default="free")
    cost_cap_usd: Mapped[float] = mapped_column(Float, default=100.0)
    timezone: Mapped[str] = mapped_column(String, default="UTC")
    working_hours: Mapped[dict] = mapped_column(JSON, default=dict)
    killed: Mapped[bool] = mapped_column(default=False)  # global kill switch
    simulation: Mapped[bool] = mapped_column(default=False)  # dry-run: no real side effects
    qualification_framework: Mapped[str] = mapped_column(String, default="BANT")  # BANT|MEDDIC
    webhook_secret_hash: Mapped[str | None] = mapped_column(String, nullable=True)  # sha256 of LeadForge secret
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String, index=True)
    pw_hash: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default="member")  # ceo|dept_head|member|client
    account_id: Mapped[str | None] = mapped_column(String, nullable=True)  # set for client users
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AgentProfile(Base):
    __tablename__ = "agent_profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String, default="echo")  # echo|anthropic
    model: Mapped[str] = mapped_column(String, default="echo-1")
    max_turns: Mapped[int] = mapped_column(Integer, default=4)
    max_tokens: Mapped[int] = mapped_column(Integer, default=1024)
    cost_ceiling_usd: Mapped[float] = mapped_column(Float, default=1.0)
    autonomy_default: Mapped[str] = mapped_column(String, default="L1")  # L0..L3
    autonomy_overrides: Mapped[dict] = mapped_column(JSON, default=dict)  # action_type -> level
    tool_grants: Mapped[list] = mapped_column(JSON, default=list)  # list[str] of tool names


class Actor(Base):
    __tablename__ = "actors"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    type: Mapped[str] = mapped_column(String)  # agent|human
    name: Mapped[str | None] = mapped_column(String, nullable=True)  # persona, e.g. "Sam Sales Agent"
    role: Mapped[str] = mapped_column(String, default="member")  # lead|head|member
    status: Mapped[str] = mapped_column(String, default="active")
    department_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    agent_profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_profiles.id"), nullable=True
    )


class ToolRegistration(Base):
    __tablename__ = "tool_registrations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    side_effect: Mapped[str] = mapped_column(String, default="read")  # read|write|irreversible
    cost_estimate_usd: Mapped[float] = mapped_column(Float, default=0.0)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), index=True)
    trace_id: Mapped[str] = mapped_column(String, index=True, default=_uuid)
    trigger: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="queued")
    turns_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    kill_requested: Mapped[bool] = mapped_column(default=False)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    charter: Mapped[str] = mapped_column(Text, default="")
    head_actor_id: Mapped[str | None] = mapped_column(ForeignKey("actors.id"), nullable=True)
    budget_usd: Mapped[float] = mapped_column(Float, default=0.0)
    paused: Mapped[bool] = mapped_column(default=False)  # per-department pause


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    goal: Mapped[str] = mapped_column(Text)
    account_id: Mapped[str | None] = mapped_column(String, nullable=True)  # client account (nullable = internal)
    owner_actor_id: Mapped[str | None] = mapped_column(ForeignKey("actors.id"), nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, default="planning")  # planning|active|done
    health: Mapped[str] = mapped_column(String, default="unknown")  # on_track|at_risk|slipping
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    parent_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    goal: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[str] = mapped_column(Text, default="")
    department_id: Mapped[str | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    assignee_actor_id: Mapped[str | None] = mapped_column(ForeignKey("actors.id"), nullable=True)
    reviewer_actor_id: Mapped[str | None] = mapped_column(ForeignKey("actors.id"), nullable=True)  # paired task
    depends_on: Mapped[list] = mapped_column(JSON, default=list)  # list[task_id]
    est_effort_hours: Mapped[float] = mapped_column(Float, default=1.0)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String, default="proposed")  # proposed|scheduled|running|done|blocked
    # computed schedule fields (recomputed by services/scheduling)
    est_start_h: Mapped[float] = mapped_column(Float, default=0.0)
    est_finish_h: Mapped[float] = mapped_column(Float, default=0.0)
    slack_h: Mapped[float] = mapped_column(Float, default=0.0)
    is_critical: Mapped[bool] = mapped_column(default=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    type: Mapped[str] = mapped_column(String, default="doc")  # doc|code|design|contract|email|plan
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text, default="")
    produced_by_actor_id: Mapped[str | None] = mapped_column(ForeignKey("actors.id"), nullable=True)
    reviewed_by_actor_id: Mapped[str | None] = mapped_column(ForeignKey("actors.id"), nullable=True)
    status: Mapped[str] = mapped_column(String, default="produced")  # produced|reviewed|approved
    critic_reasons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    needs_human: Mapped[bool] = mapped_column(default=False)
    blocked: Mapped[bool] = mapped_column(default=False)  # Legal veto — human-override only
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    playbook_version: Mapped[int | None] = mapped_column(Integer, nullable=True)  # A/B attribution
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Playbook(Base):
    __tablename__ = "playbooks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"), index=True)
    title: Mapped[str] = mapped_column(String)
    markdown: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="active")  # active|draft|superseded
    change_summary: Mapped[str] = mapped_column(Text, default="")
    effective_from: Mapped[datetime] = mapped_column(DateTime, default=_now)


class HandoffPacket(Base):
    __tablename__ = "handoff_packets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    from_department_id: Mapped[str | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    to_department_id: Mapped[str | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    context: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list] = mapped_column(JSON, default=list)  # list[str] (artifact refs / content)
    open_questions: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Thread(Base):
    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    thread_type: Mapped[str] = mapped_column(String)  # request|handoff|escalation|status|client
    subject: Mapped[str] = mapped_column(String, default="")
    message_budget: Mapped[int] = mapped_column(Integer, default=6)
    status: Mapped[str] = mapped_column(String, default="open")  # open|resolved|escalated
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id"), index=True)
    sender_actor_id: Mapped[str | None] = mapped_column(ForeignKey("actors.id"), nullable=True)
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Annotation(Base):
    """Human review note on an artifact. May propose a Playbook amendment."""

    __tablename__ = "annotations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)
    author_actor_id: Mapped[str | None] = mapped_column(ForeignKey("actors.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    proposed_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    amendment_playbook_id: Mapped[str | None] = mapped_column(ForeignKey("playbooks.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Event(Base):
    """Append-only audit log. Source of truth. No update/delete path in the app."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    trace_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("actors.id"), nullable=True)
    seq: Mapped[int] = mapped_column(Integer)  # per-trace ordering, monotonic
    action: Mapped[str] = mapped_column(String)  # run.started, model.call, tool.call, run.succeeded...
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    simulated: Mapped[bool] = mapped_column(default=False)  # dry-run action, no real effect
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    domain: Mapped[str | None] = mapped_column(String, nullable=True)
    industry: Mapped[str | None] = mapped_column(String, nullable=True)
    size_employees: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_client: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    name: Mapped[str] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    source: Mapped[str] = mapped_column(String, default="inbound")
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    company: Mapped[str] = mapped_column(String, default="")
    industry: Mapped[str | None] = mapped_column(String, nullable=True)
    size_employees: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)  # budget, authority, need, timeline, notes...
    icp_fit_score: Mapped[int] = mapped_column(Integer, default=0)
    qualification_state: Mapped[str] = mapped_column(String, default="new")  # new|qualified|disqualified|insufficient_info
    disqualify_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    framework: Mapped[str] = mapped_column(String, default="BANT")
    evidence: Mapped[list] = mapped_column(JSON, default=list)  # [{claim, present, evidence, source}]
    handoff_packet_id: Mapped[str | None] = mapped_column(ForeignKey("handoff_packets.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ChangeOrder(Base):
    __tablename__ = "change_orders"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="open")  # open|accepted|rejected
    handoff_packet_id: Mapped[str | None] = mapped_column(ForeignKey("handoff_packets.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class MemoryRecord(Base):
    """Shared, scoped knowledge. Agents write what they produced here and read it before working,
    so the team builds on each other's output instead of guessing in isolation.
    """
    __tablename__ = "memory_records"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    scope: Mapped[str] = mapped_column(String, default="project")  # project|department|org|client
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    department_id: Mapped[str | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    source_actor_id: Mapped[str | None] = mapped_column(ForeignKey("actors.id"), nullable=True)
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Scorecard(Base):
    __tablename__ = "scorecards"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), index=True)
    period: Mapped[str] = mapped_column(String, default="all")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    direction: Mapped[str] = mapped_column(String, default="inbound")  # inbound|outbound
    from_number: Mapped[str | None] = mapped_column(String, nullable=True)
    to_number: Mapped[str | None] = mapped_column(String, nullable=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    lead_id: Mapped[str | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    consent: Mapped[bool] = mapped_column(default=False)
    recording_ref: Mapped[str | None] = mapped_column(String, nullable=True)  # stored only with consent
    transcript: Mapped[str] = mapped_column(Text, default="")
    extracted_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String, default="")  # qualified|follow_up|not_interested
    follow_up_project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    condition: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g. {"action_type": "client_send"}
    effect: Mapped[str] = mapped_column(String)  # allow|require_approval|deny
    priority: Mapped[int] = mapped_column(Integer, default=0)  # higher wins
    active: Mapped[bool] = mapped_column(default=True)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    action_type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    preview: Mapped[str] = mapped_column(Text, default="")
    requested_by_actor_id: Mapped[str | None] = mapped_column(ForeignKey("actors.id"), nullable=True)
    department_id: Mapped[str | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|approved|rejected|expired
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approver_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
