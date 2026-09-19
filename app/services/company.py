"""The company's own context, handed to every agent.

Agents were told "never write [Company Name] placeholders" but were never told the company's name or
what it does, so they wrote placeholders anyway, the Critic (correctly) rejected them, and most work
ended "needs human review". This is the fix at the source: state the facts once, inject them everywhere.
"""
from sqlalchemy.orm import Session

from app.models import Organization

PROFILE_MAX_CHARS = 4000  # it rides along in every prompt, so it is bounded


def company_context(db: Session, org_id: str) -> str:
    """A prompt block naming the company and (if the CEO wrote one) its profile. Empty string only if
    the org doesn't exist. The name alone is worth sending: it removes [Company Name]."""
    org = db.get(Organization, org_id)
    if org is None:
        return ""
    lines = [f"About the company you work for: {org.name}."]
    profile = (org.profile or "").strip()
    if profile:
        lines.append(profile[:PROFILE_MAX_CHARS])
    lines.append(
        "Use these facts directly - write the real company name, never [Company Name] or similar. "
        "Do not invent facts about the company that are not stated here."
    )
    return "\n".join(lines)
