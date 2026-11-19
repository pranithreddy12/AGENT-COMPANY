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
    prices = (org.price_list or "").strip()
    if prices:
        lines.append("Approved prices (use ONLY these amounts, exactly as written):\n" + prices[:PROFILE_MAX_CHARS])
    else:
        lines.append("No price list is configured: write [price to confirm] wherever a price would go.")
    proof = (org.proof_points or "").strip()
    if proof:
        lines.append("Real results you may cite (cite ONLY these; never invent a statistic, client or "
                     "outcome):\n" + proof[:PROFILE_MAX_CHARS])
    else:
        lines.append("No proof points are configured: do not claim specific results, client names or "
                     "statistics - describe the approach instead.")
    lines.append(
        "Use these facts directly - write the real company name, never [Company Name] or similar. "
        "Do not invent facts about the company that are not stated here. "
        "Never state a number about the reader or their business (their revenue, losses, patient counts, "
        "percentages, time saved) - you do not know it. Use only prices and results given above; otherwise "
        "ask a question or describe the approach without figures. "
        "If some other detail is genuinely not given (e.g. which client a generic template is for), write it as a "
        "double-brace merge field like {{client_name}} - never in square brackets, and never invent it."
    )
    return "\n".join(lines)
