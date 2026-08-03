"""Lead qualification + scope-change detection. Deterministic: scoring and routing are code, not
model calls. Every claim carries cited evidence; missing data is cited as a gap, never invented.
"""
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChangeOrder, Department, Lead, Organization, Project
from app.services import communication

# ICP config (org-level in a later phase; constant here).
# ponytail: hardcoded ICP; move to org settings when tenants need different ICPs.
ICP_INDUSTRIES = {"software", "saas", "fintech", "healthcare", "ecommerce"}
ICP_MIN_SIZE, ICP_MAX_SIZE = 20, 5000
ICP_THRESHOLD = 50

# Framework dimensions -> the attribute key that evidences them.
FRAMEWORKS = {
    "BANT": {"Budget": "budget", "Authority": "authority", "Need": "need", "Timeline": "timeline"},
    "MEDDIC": {"Metrics": "metrics", "EconomicBuyer": "economic_buyer", "DecisionCriteria": "decision_criteria",
               "DecisionProcess": "decision_process", "IdentifyPain": "pain", "Champion": "champion"},
}


def icp_fit(lead: Lead) -> int:
    score = 0
    if (lead.industry or "").lower() in ICP_INDUSTRIES:
        score += 50
    if lead.size_employees and ICP_MIN_SIZE <= lead.size_employees <= ICP_MAX_SIZE:
        score += 50
    return score


def _evidence(framework: str, attributes: dict) -> list[dict]:
    claims = []
    for dim, key in FRAMEWORKS[framework].items():
        val = attributes.get(key)
        present = bool(val)
        claims.append({
            "claim": dim,
            "present": present,
            "evidence": str(val) if present else "no data provided",
            "source": f"lead.attributes.{key}",  # citation
        })
    return claims


def qualify(db: Session, lead: Lead) -> Lead:
    org = db.get(Organization, lead.org_id)
    framework = org.qualification_framework if org else "BANT"
    lead.framework = framework
    lead.icp_fit_score = icp_fit(lead)
    lead.evidence = _evidence(framework, lead.attributes or {})

    present = [c for c in lead.evidence if c["present"]]
    total = len(lead.evidence)
    lead.confidence = round(len(present) / total, 3) if total else 0.0

    if lead.icp_fit_score < ICP_THRESHOLD:
        lead.qualification_state = "disqualified"
        lead.disqualify_reason = f"ICP fit below threshold (score {lead.icp_fit_score})"
        return lead

    if len(present) < math.ceil(total / 2):
        missing = [c["claim"] for c in lead.evidence if not c["present"]]
        lead.qualification_state = "insufficient_info"
        lead.disqualify_reason = "insufficient information: " + ", ".join(missing)
        return lead

    # qualified -> structured handoff to Sales with cited evidence
    sales = db.scalars(select(Department).where(Department.org_id == lead.org_id, Department.name == "Sales")).first()
    packet = communication.make_handoff(
        db, org_id=lead.org_id, project_id=None,  # not yet a project
        from_dept=None, to_dept=sales.id if sales else None,
        context=f"Qualified lead: {lead.company} (ICP {lead.icp_fit_score}, confidence {lead.confidence})",
        evidence=[f"{c['claim']}: {c['evidence']} [{c['source']}]" for c in present],
        open_questions=[c["claim"] for c in lead.evidence if not c["present"]],
        confidence=lead.confidence,
    )
    lead.handoff_packet_id = packet.id
    lead.qualification_state = "qualified"
    lead.disqualify_reason = None
    return lead


# --- scope-change detection ---

_SCOPE_MARKERS = ("also want", "additionally", "add a", "add an", "new feature", "expand", "on top of",
                  "increase scope", "one more", "as well")


def detect_scope_change(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _SCOPE_MARKERS)


def raise_change_order(db: Session, project: Project, description: str) -> ChangeOrder:
    """Route an out-of-SOW request to Sales as a change order rather than absorbing it silently."""
    sales = db.scalars(select(Department).where(Department.org_id == project.org_id, Department.name == "Sales")).first()
    packet = communication.make_handoff(
        db, org_id=project.org_id, project_id=project.id, from_dept=None,
        to_dept=sales.id if sales else None,
        context=f"Scope change on {project.goal[:40]}: {description[:80]}",
        evidence=[description], open_questions=["Price and timeline for the change?"],
    )
    co = ChangeOrder(org_id=project.org_id, project_id=project.id, account_id=project.account_id,
                     description=description, status="open", handoff_packet_id=packet.id)
    db.add(co)
    db.flush()
    return co
