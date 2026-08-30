"""Bootstrap endpoint: create an org and seed a demo echo agent + granted tools.

Open (no auth) — this is how a tenant is first created. Returns a CEO access token.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from fastapi import HTTPException

from app.auth import hash_password, issue_token, verify_password
from app.db import get_db
from app.models import Actor, AgentProfile, Department, Organization, Playbook, Policy, User
from app.schemas import LoginRequest, OrgCreate, OrgCreated, TokenOut
from app.services import tools
from sqlalchemy import select

router = APIRouter(tags=["orgs"])

# name -> charter. The Lead routes tasks to these by name.
DEPARTMENTS = {
    "Planning": "Turns projects into schedules, owns the critical path, runs retros.",
    "Sales": "Pipeline, proposals, pricing within guardrails, follow-up cadence.",
    "Marketing": "Positioning, content, campaign briefs, copy. Never publishes without approval.",
    "Development": "Technical scoping, architecture, implementation specs, review, QA.",
    "Legal": "Contract drafting, clause review, risk flags, compliance. Has veto power over shipping.",
    "Client Management": "Owns the client relationship, status updates, scope-change detection.",
}

# Non-negotiable output quality bar — every agent's prompt ends with this, regardless of persona.
_HARD_RULES = (
    "Hard rules: use the real specifics from the context — NEVER write placeholders like "
    "[Company Name], [Insert X], [Date], or 'Example'. Be concrete and actionable: specific steps, "
    "real numbers and targets, named tactics and decisions — not generic advice. Do not restate the "
    "task or narrate what you're about to do; just deliver."
)

# persona name -> (department, distinct personality). Each agent gets its OWN AgentProfile row (not
# a shared one) so a genuinely different voice/working style comes through, and so editing one
# agent's prompt (Governance -> Agents) can never silently rewrite six other agents at once.
PERSONAS: dict[str, tuple[str, str]] = {
    "Piper Planning Agent": ("Planning",
        "You are Piper, the Planning specialist at a consulting agency. You think in dependencies, "
        "timelines, and critical paths — every plan you write names what happens first, what can run "
        "in parallel, and what blocks what. You'd rather flag a scheduling risk early than let it "
        "slip silently."),
    "Sam Sales Agent": ("Sales",
        "You are Sam, the Sales specialist at a consulting agency. You write with conviction and "
        "clarity — every proposal leads with the client's actual pain and ties each recommendation to "
        "a concrete business outcome. You're direct, not pushy: you'd rather lose a deal honestly than "
        "oversell it."),
    "Mia Marketing Agent": ("Marketing",
        "You are Mia, the Marketing specialist at a consulting agency. You write for the actual "
        "reader, not the brief — sharp hooks, plain language, no jargon for its own sake. You care "
        "about what makes someone stop scrolling and believe what they're reading."),
    "Devin Dev Agent": ("Development",
        "You are Devin, a Development specialist at a consulting agency. You're pragmatic and "
        "implementation-first: you scope the smallest thing that actually works and call out real "
        "tradeoffs (time vs. correctness vs. scope) instead of hand-waving a technical detail."),
    "Dana Dev Agent": ("Development",
        "You are Dana, a Development specialist at a consulting agency. You think about what breaks: "
        "edge cases, failure modes, and what a shortcut costs six months after ship. You'd rather flag "
        "a design risk now than let it become production debt later."),
    "Lena Legal Agent": ("Legal",
        "You are Lena, the Legal specialist at a consulting agency. You are precise and risk-averse by "
        "design — you flag anything that could expose the client or the agency, and you never soften a "
        "real compliance concern just to make a deal move faster."),
    "Cleo Client Agent": ("Client Management",
        "You are Cleo, the Client Management specialist at a consulting agency. You protect the "
        "relationship: warm, direct, and honest with clients, and quick to flag scope creep or a "
        "missed expectation before it becomes a bigger problem."),
}


@router.post("/login", response_model=TokenOut)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenOut:
    user = db.scalars(select(User).where(User.email == body.email)).first()
    if user is None or not verify_password(body.password, user.pw_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    return TokenOut(access_token=issue_token(user), role=user.role)


@router.post("/orgs", response_model=OrgCreated)
def create_org(body: OrgCreate, db: Session = Depends(get_db)) -> OrgCreated:
    org = Organization(name=body.name)
    db.add(org)
    db.flush()

    ceo = User(org_id=org.id, email=body.ceo_email, pw_hash=hash_password(body.ceo_password), role="ceo")
    db.add(ceo)

    tools.register_builtins(db, org.id, ["echo", "get_time", "web_search"])

    # Default governance policies. Higher priority wins; deny beats require_approval.
    db.add_all([
        Policy(org_id=org.id, name="forbidden-claims", effect="deny", priority=100,
               condition={"contains_any": ["guaranteed returns", "no risk", "guarantee"]}),
        Policy(org_id=org.id, name="client-send-requires-approval", effect="require_approval",
               priority=50, condition={"action_type": "client_send"}),
    ])

    # The Lead (Chief of Staff) — plans and routes, never does the work.
    lead_profile = AgentProfile(
        org_id=org.id, name="Lead", system_prompt="You are the Chief of Staff. Decompose goals into a task DAG.",
        provider="echo", model="echo-1", max_turns=2, tool_grants=[],
    )
    db.add(lead_profile)
    db.flush()
    lead = Actor(org_id=org.id, type="agent", role="lead", name="Cora Lead Agent", agent_profile_id=lead_profile.id)
    db.add(lead)
    db.flush()

    depts_by_name: dict[str, Department] = {}
    for name, charter in DEPARTMENTS.items():
        dept = Department(org_id=org.id, name=name, charter=charter)
        db.add(dept)
        db.flush()
        db.add(Playbook(
            org_id=org.id, department_id=dept.id, title=f"{name} SOP v1", version=1,
            markdown=f"# {name} Playbook\n\n{charter}\n\n- Produce artifacts that meet the task acceptance criteria.\n- Escalate to a human when uncertain.\n\nRULE: Be specific to the actual client and goal — concrete numbers, named tactics, real recommendations. No placeholders, no generic templates.",
        ))
        depts_by_name[name] = dept

    # Each department agent gets its OWN AgentProfile — a distinct personality, not a shared one — so
    # editing one agent (Governance -> Agents) can never silently rewrite the others, and each agent
    # actually sounds like a different person instead of seven names on one clone.
    first_agent = None
    for persona, (dept_name, voice) in PERSONAS.items():
        profile = AgentProfile(
            org_id=org.id, name=persona, system_prompt=f"{voice} {_HARD_RULES}",
            provider="echo", model="echo-1", max_turns=4,
            tool_grants=["echo", "get_time", "web_search"],
        )
        db.add(profile)
        db.flush()
        a = Actor(org_id=org.id, type="agent", role="member", name=persona,
                  agent_profile_id=profile.id, department_id=depts_by_name[dept_name].id)
        db.add(a)
        db.flush()
        first_agent = first_agent or a

    # Cross-cutting Critic actor (reviews artifacts against acceptance criteria + Playbook).
    critic_profile = AgentProfile(
        org_id=org.id, name="Critic", system_prompt="You are the QA Critic. Pass or return revision reasons.",
        provider="echo", model="echo-1", max_turns=2, tool_grants=[],
    )
    db.add(critic_profile)
    db.flush()
    db.add(Actor(org_id=org.id, type="agent", role="critic", name="Quinn QA Agent", agent_profile_id=critic_profile.id))

    # Research agent — searches the web at the start of every project and shares findings with the team.
    research_profile = AgentProfile(
        org_id=org.id, name="Researcher",
        system_prompt="You are the Research agent. Produce sourced, factual research briefs.",
        provider="echo", model="echo-1", max_turns=2, tool_grants=["web_search"],
    )
    db.add(research_profile)
    db.flush()
    db.add(Actor(org_id=org.id, type="agent", role="research", name="Rex Research Agent",
                 agent_profile_id=research_profile.id))
    db.commit()

    # actor_id returned = first worker agent (handy for the Phase 0 trivial-run demo).
    return OrgCreated(org_id=org.id, actor_id=first_agent.id, access_token=issue_token(ceo))
