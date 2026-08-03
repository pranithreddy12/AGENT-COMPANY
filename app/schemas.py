from datetime import datetime

from pydantic import BaseModel


class OrgCreate(BaseModel):
    name: str
    ceo_email: str
    ceo_password: str


class OrgCreated(BaseModel):
    org_id: str
    actor_id: str  # the seeded demo echo agent
    access_token: str


class RunCreate(BaseModel):
    actor_id: str
    input: str


class RunOut(BaseModel):
    id: str
    trace_id: str
    status: str
    turns_used: int
    cost_usd: float
    result: dict | None
    error: str | None


class EventOut(BaseModel):
    seq: int
    action: str
    target: str | None
    before: dict | None
    after: dict | None
    cost_usd: float
    latency_ms: int


class TraceOut(BaseModel):
    run: RunOut
    events: list[EventOut]


# --- Phase 1: work layer ---

class ProjectCreate(BaseModel):
    goal: str
    account_id: str | None = None


class TaskOut(BaseModel):
    id: str
    goal: str
    acceptance_criteria: str
    department_id: str | None
    assignee_actor_id: str | None
    depends_on: list[str]
    est_effort_hours: float
    status: str
    est_start_h: float
    est_finish_h: float
    slack_h: float
    is_critical: bool
    due_at: datetime | None


class ProjectOut(BaseModel):
    id: str
    goal: str
    status: str
    health: str
    start_at: datetime
    due_at: datetime | None
    critical_path: list[str] = []
    tasks: list[TaskOut] = []


class ArtifactOut(BaseModel):
    id: str
    task_id: str
    type: str
    content: str
    version: int
    status: str
    critic_reasons: list[str] | None
    needs_human: bool
    blocked: bool
    block_reason: str | None


class SlipRequest(BaseModel):
    added_hours: float


class HandoffOut(BaseModel):
    id: str
    from_department_id: str | None
    to_department_id: str | None
    context: str
    evidence: list[str]
    open_questions: list[str]
    confidence: float


class MessageOut(BaseModel):
    sender_actor_id: str | None
    content: str


class ThreadOut(BaseModel):
    id: str
    thread_type: str
    subject: str
    status: str
    message_budget: int
    messages: list[MessageOut]


# --- Phase 3: governance ---

class OutboundRequest(BaseModel):
    actor_id: str
    action_type: str = "client_send"
    intent: str


class DecideRequest(BaseModel):
    decision: str  # approve | reject
    reason: str | None = None


class ApprovalOut(BaseModel):
    id: str
    action_type: str
    preview: str
    status: str
    requested_by_actor_id: str | None
    decision_reason: str | None


# --- Phase 4: human layer ---

class TeamMemberCreate(BaseModel):
    email: str
    password: str
    department_id: str
    role: str = "member"  # member | dept_head


class TeamMemberOut(BaseModel):
    user_id: str
    actor_id: str
    access_token: str


class AssignRequest(BaseModel):
    assignee_actor_id: str | None = None
    reviewer_actor_id: str | None = None


class AnnotateRequest(BaseModel):
    text: str
    proposed_rule: str | None = None


class PlaybookOut(BaseModel):
    id: str
    department_id: str
    version: int
    status: str
    change_summary: str
    markdown: str


# --- Phase 5: client-facing ---

class LoginRequest(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    role: str


class LeadCreate(BaseModel):
    source: str = "inbound"
    company: str
    industry: str | None = None
    size_employees: int | None = None
    attributes: dict = {}


class LeadOut(BaseModel):
    id: str
    company: str
    qualification_state: str
    icp_fit_score: int
    confidence: float
    framework: str
    disqualify_reason: str | None
    evidence: list[dict]
    handoff_packet_id: str | None


class ClientCreate(BaseModel):
    account_name: str
    email: str
    password: str
    industry: str | None = None


class ClientCreated(BaseModel):
    account_id: str
    user_id: str
    access_token: str


class PortalMessage(BaseModel):
    text: str


class PortalProjectOut(BaseModel):
    id: str
    goal: str
    status: str
    health: str
    deliverables: list[ArtifactOut]
    messages: list[MessageOut]


# --- Phase 6: voice ---

class CallCreate(BaseModel):
    direction: str = "inbound"
    from_number: str | None = None
    company: str
    transcript: str
    consent: bool = False
    recording_ref: str | None = None
    industry: str | None = None
    size_employees: int | None = None


class FollowUpTaskOut(BaseModel):
    id: str
    goal: str
    department_id: str | None
    assignee_actor_id: str | None
    status: str


class CallOut(BaseModel):
    id: str
    direction: str
    consent: bool
    recording_ref: str | None
    summary: str
    outcome: str
    extracted_fields: dict
    lead_id: str | None
    lead_state: str | None
    follow_up_project_id: str | None
    follow_up_tasks: list[FollowUpTaskOut]


# --- Phase 7: intelligence ---

class HireRequest(BaseModel):
    job_description: str
    department_id: str | None = None


class HireOut(BaseModel):
    profile_id: str
    name: str
    eval: dict


class ConfirmHireRequest(BaseModel):
    department_id: str | None = None
