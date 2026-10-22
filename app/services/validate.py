"""Deterministic output checks. Code, not a model: the rule is expressible, so it is enforced exactly.

Models draft; this decides whether a draft is even eligible to be sent. It catches the failures that
kept ending in "needs human review" (placeholders, invented prices, dates in the past) and returns a
SPECIFIC fix instruction per problem, so a retry is targeted instead of a vague "revise".
"""
import re
from dataclasses import dataclass
from datetime import date

# a bracketed fill-in: [Company Name], [Insert X], [Date], [Your Name]. Allowed: the one sanctioned
# price marker, markdown links "[text](url)" and checkbox "[ ]" / "[x]".
_PLACEHOLDER = re.compile(r"\[(?!\s*\]|x\]|X\])([^\[\]\n]{1,60})\](?!\()")
_SANCTIONED = {"price to confirm"}
_FILLER_WORDS = re.compile(r"\b(lorem ipsum|insert (?:here|name|date|company)|your (?:name|company|agency) here)\b", re.I)

# currency amounts: $1,200  $1.2k  USD 500  1200 USD  AED 3,000  EUR / GBP / INR variants
_MONEY = re.compile(
    r"(?:(?:USD|AED|EUR|GBP|INR|\$|€|£|₹)\s?(\d[\d,]*(?:\.\d+)?)\s?([kKmM])?)"
    r"|(?:(\d[\d,]*(?:\.\d+)?)\s?([kKmM])?\s?(?:USD|AED|EUR|GBP|INR)\b)"
)
_YEAR = re.compile(r"\b(19|20)\d{2}\b")


@dataclass
class Issue:
    kind: str  # placeholder | price | date | filler
    detail: str
    fix: str


def _amount(num: str, suffix: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * {"k": 1e3, "m": 1e6}.get((suffix or "").lower(), 1)


def amounts_in(text: str) -> list[float]:
    out = []
    for m in _MONEY.finditer(text or ""):
        num, suf = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        try:
            out.append(_amount(num, suf))
        except ValueError:
            continue
    return out


def check(text: str, *, price_list: str | None = None, today: date | None = None,
          require_price_list: bool = False) -> list[Issue]:
    """All deterministic problems in `text`. Empty list = eligible.

    price_list: the org's own prices (any text containing amounts). Every amount in the draft must be
      one of them (or derivable: <= the largest listed, to allow sums is deliberately NOT done - a sum
      is a new number a human has not approved). With no list configured, amounts are only flagged when
      require_price_list is set (proposals), because an invented quote is the costly failure.
    today: for the "date in the past" check (defaults to the real date).
    """
    issues: list[Issue] = []
    text = text or ""
    today = today or date.today()

    seen = set()
    for m in _PLACEHOLDER.finditer(text):
        label = m.group(1).strip()
        if label.lower() in _SANCTIONED or label.lower() in seen:
            continue
        seen.add(label.lower())
        issues.append(Issue("placeholder", f"[{label}]",
                            f"Replace [{label}] with the real value from the company profile, or remove that line. "
                            "If the value genuinely is not known, say so plainly instead of leaving a bracket."))
    if _FILLER_WORDS.search(text):
        issues.append(Issue("filler", _FILLER_WORDS.search(text).group(0),
                            "Remove filler / template wording; write the actual content."))

    found = amounts_in(text)
    if found:
        allowed = set(amounts_in(price_list or ""))
        if price_list and price_list.strip():
            bad = sorted({a for a in found if a not in allowed})
            if bad:
                shown = ", ".join(f"{b:g}" for b in bad)
                issues.append(Issue("price", f"amount(s) not in the price list: {shown}",
                                    "Use only amounts from the company's price list, exactly as listed. "
                                    "For anything else write [price to confirm] - never invent or sum a quote."))
        elif require_price_list:
            issues.append(Issue("price", "a specific price with no price list configured",
                                "No price list is set, so no figure is approved. Write [price to confirm] instead of a number."))

    for m in _YEAR.finditer(text):
        y = int(m.group(0))
        if 1990 <= y < today.year:
            # a past year is fine as history ("founded 2019", "in 2023 we...") - flag only when the
            # surrounding words make it a schedule/deadline
            ctx = text[max(0, m.start() - 60): m.end() + 20].lower()
            if re.search(r"\b(deadline|launch|deliver|due|by|until|timeline|start|kick-?off|go-?live|milestone|complete)\b", ctx):
                issues.append(Issue("date", f"{y} used as a schedule date",
                                    f"Today is {today.isoformat()}. Schedules must be in the future; correct the year."))
                break
    return issues


def as_feedback(issues: list[Issue]) -> str:
    """The retry instruction for a producer: numbered, specific, deterministic."""
    return "Automated checks failed. Fix EVERY point, keep everything else:\n" + "\n".join(
        f"{i}. {x.detail}: {x.fix}" for i, x in enumerate(issues, 1)
    )
