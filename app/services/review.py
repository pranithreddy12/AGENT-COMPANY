"""Critic (QA) + Legal veto. Deterministic in demo mode; the real path would use an LLM judge.

Both return structured verdicts. The Critic's revise loop is capped by the caller (planning),
which is what makes it a bounded loop rather than an open-ended negotiation.
"""
import json
import re
from dataclasses import dataclass


@dataclass
class Verdict:
    passed: bool
    reasons: list[str]
    # True when the JUDGE failed (unparseable reply), not the work. The caller must not send the
    # producing agent back to revise something nobody actually critiqued.
    error: bool = False


_CRITIC_SYSTEM = (
    "You are a demanding QA reviewer at a top agency. Judge whether the artifact is genuinely useful "
    "and ready to hand to a client, against the acceptance criteria and the department Playbook. "
    "FAIL it (passed=false) if it contains placeholders like [Company Name], [Insert...], [Date] or "
    "'Example' (double-brace merge fields like {{client_name}} ARE acceptable where the task does not say who "
    "the recipient is); is a generic template that could apply to any client; restates the task instead of "
    "doing it; or gives vague advice with no concrete specifics. PASS only if it is specific to the "
    "goal, concrete, and actionable. Respond ONLY with JSON: {\"passed\": bool, \"reasons\": [string]} "
    "— reasons name concrete fixes. Give AT MOST 4 reasons, one short sentence each."
)


def _salvage(text: str) -> Verdict:
    """The judge's JSON was cut off mid-list (observed live: a long list of reasons hit the token cap) or
    wrapped in prose. A clear verdict is still in there: pull "passed" and any COMPLETE reason strings.
    Still fails closed - no recognisable "passed" means an error verdict, never a silent pass."""
    m = re.search(r'"passed"\s*:\s*(true|false)', text or "", re.I)
    if not m:
        return Verdict(False, ["QA review unavailable: the critic's reply could not be parsed"], error=True)
    passed = m.group(1).lower() == "true"
    i = text.lower().find('"reasons"')
    tail = text[i + len('"reasons"'):] if i >= 0 else ""
    reasons = [r.strip() for r in re.findall(r'"((?:[^"\\]|\\.){8,})"', tail)]
    return Verdict(passed, [] if passed else (reasons or ["revision requested"]))


def parse_critic_verdict(text: str) -> Verdict:
    """Parse a model's critic response. Pure — no LLM. Fails closed (revise) on unparseable output
    so a malformed judge response never silently passes an artifact."""
    from app.services.llm import extract_json_object
    try:
        data = json.loads(extract_json_object(text))
        passed = bool(data["passed"])
        reasons = [str(r) for r in data.get("reasons", [])]
    except (json.JSONDecodeError, KeyError, TypeError):
        return _salvage(text)
    return Verdict(passed, [] if passed else (reasons or ["revision requested"]))


def llm_critic_review(provider, content: str, acceptance_criteria: str, playbook: str, max_tokens: int = 1024) -> Verdict:
    """Real Critic: an LLM judges the artifact. Same Verdict interface as the deterministic critic."""
    rubric = (f"Acceptance criteria: {acceptance_criteria}\n\nPlaybook:\n{playbook}\n\n"
              f"Artifact under review:\n{content}")
    from app.services.llm import complete_with_retry

    messages = [{"role": "user", "content": rubric}]
    verdict = Verdict(False, [], error=True)
    for _ in range(2):  # weak models often wrap or garble the JSON once; one stricter retry fixes most
        try:  # JSON mode makes weak models actually return JSON; providers without it just don't get the kwarg
            comp = complete_with_retry(provider, system=_CRITIC_SYSTEM, messages=messages, tools=[],
                                       max_tokens=max_tokens, json_mode=True)
        except TypeError as e:
            if "json_mode" not in str(e):
                raise
            comp = complete_with_retry(provider, system=_CRITIC_SYSTEM, messages=messages, tools=[],
                                       max_tokens=max_tokens)
        verdict = parse_critic_verdict(comp.text or "")
        if not verdict.error:
            return verdict
        messages = messages + [
            {"role": "assistant", "content": comp.text or ""},
            {"role": "user", "content": 'Reply with ONLY a JSON object: {"passed": true|false, "reasons": []}.'},
        ]
    return verdict


def critic_review(content: str, acceptance_criteria: str, playbook: str) -> Verdict:
    """Pass when the artifact has real content. Demo hook: acceptance criteria containing
    'IMPOSSIBLE' always fails — used to exercise the revise cap + escalation deterministically.
    """
    if "IMPOSSIBLE" in (acceptance_criteria or ""):
        return Verdict(False, ["acceptance criteria cannot be satisfied by this artifact"])
    if not content or not content.strip():
        return Verdict(False, ["artifact is empty"])
    return Verdict(True, [])


# A deliberately small keyword screen — NOT a substitute for legal/compliance review. It catches a
# few obviously-unsendable phrases; the human approval gate (see integrations.proposal_view /
# approve_proposal) is what actually clears anything for sending. Grow this list, or swap in an LLM
# compliance check, if the stakes rise.
PROHIBITED_MARKERS = ("PROHIBITED", "guaranteed returns", "no risk")


def legal_review(content: str) -> Verdict:
    """Coarse keyword screen: veto when the content contains a prohibited marker. This is a safety
    net, not a real legal review — a human must still approve a proposal before anything is sent."""
    hits = [m for m in PROHIBITED_MARKERS if m.lower() in (content or "").lower()]
    if hits:
        return Verdict(False, [f"prohibited content: {', '.join(hits)}"])
    return Verdict(True, [])
