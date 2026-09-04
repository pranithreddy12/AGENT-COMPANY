"""Provider-abstracted LLM layer. Every call is bounded (max_tokens, timeout).

- EchoProvider: deterministic, zero external cost. Powers tests and the Phase 0 demo.
- AnthropicProvider: real. Imported lazily so `anthropic` + a key are optional until used.
"""

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Completion:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end"  # "end" | "tool_use"


@dataclass
class PlanResult:
    tasks: list[
        dict
    ]  # each: temp_id, goal, acceptance_criteria, department, est_effort_hours, depends_on[]
    input_tokens: int = 0
    output_tokens: int = 0


# Deterministic demo decomposition: a cross-department DAG. Spans six departments so cross-team
# handoffs and the critical path are both non-trivial. t_test (Development) carries slack.
_ECHO_PLAN = [
    {
        "temp_id": "t1",
        "goal": "Qualify client and confirm scope",
        "acceptance_criteria": "Scope confirmed with client",
        "department": "Sales",
        "est_effort_hours": 2.0,
        "depends_on": [],
    },
    {
        "temp_id": "t2",
        "goal": "Draft technical spec",
        "acceptance_criteria": "Spec covers confirmed scope",
        "department": "Development",
        "est_effort_hours": 5.0,
        "depends_on": ["t1"],
    },
    {
        "temp_id": "t3",
        "goal": "Review contract and compliance",
        "acceptance_criteria": "No legal or compliance blockers",
        "department": "Legal",
        "est_effort_hours": 3.0,
        "depends_on": ["t1"],
    },
    {
        "temp_id": "t_test",
        "goal": "Draft test plan",
        "acceptance_criteria": "Test plan maps to spec",
        "department": "Development",
        "est_effort_hours": 2.0,
        "depends_on": ["t2"],
    },
    {
        "temp_id": "t4",
        "goal": "Draft launch announcement copy",
        "acceptance_criteria": "Copy on-message, no unverified claims",
        "department": "Marketing",
        "est_effort_hours": 2.0,
        "depends_on": ["t2"],
    },
    {
        "temp_id": "t5",
        "goal": "Finalize delivery schedule",
        "acceptance_criteria": "Schedule agreed and dated",
        "department": "Planning",
        "est_effort_hours": 2.0,
        "depends_on": ["t2", "t3"],
    },
    {
        "temp_id": "t6",
        "goal": "Compile client status update",
        "acceptance_criteria": "Update approved for client",
        "department": "Client Management",
        "est_effort_hours": 2.0,
        "depends_on": ["t4", "t5"],
    },
]

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "temp_id": {"type": "string"},
                    "goal": {"type": "string"},
                    "acceptance_criteria": {"type": "string"},
                    "department": {"type": "string"},
                    "est_effort_hours": {"type": "number"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "temp_id",
                    "goal",
                    "department",
                    "est_effort_hours",
                    "depends_on",
                ],
            },
        }
    },
    "required": ["tasks"],
}


def _tok(text: str) -> int:
    return max(1, len(text.split()))


class PlanParseError(Exception):
    pass


def extract_json_object(text: str) -> str:
    """Pull the first balanced {...} object out of a model response — local models often wrap JSON
    in prose or ```json fences. Brace-matched, not regex, so nested objects survive."""
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


_FENCED_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def strip_narrated_tool_calls(content: str) -> str:
    """Some local/quantized models don't reliably use real structured tool_calls through an
    OpenAI-compatible endpoint — instead of calling a tool, they narrate what the call would look
    like as a fenced JSON block sitting right inside otherwise-good prose (observed live: a real
    deliverable with a stray {"type": "function", "function": {"name": "web_search", ...}} block
    embedded in it). That block is noise, not content — strip it, but leave the surrounding prose
    (the actual work) alone rather than discarding the whole turn."""
    def repl(m: re.Match) -> str:
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            return m.group(0)
        fn = data.get("function") if isinstance(data.get("function"), dict) else data
        if isinstance(fn, dict) and isinstance(fn.get("name"), str) and isinstance(fn.get("arguments"), dict):
            return ""
        return m.group(0)
    return _FENCED_JSON_BLOCK.sub(repl, content).strip()


def validate_plan(data: object) -> list[dict]:
    """Validate a model-produced plan against the schema. Pure — no LLM. Fails closed on any
    malformed structure so a bad decomposition never reaches the task materializer."""
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("tasks"), list)
        or not data["tasks"]
    ):
        raise PlanParseError("missing non-empty 'tasks' array")
    tasks = data["tasks"]
    seen: set[str] = set()
    for t in tasks:
        if not isinstance(t, dict):
            raise PlanParseError("task is not an object")
        for k in ("temp_id", "goal", "department", "est_effort_hours", "depends_on"):
            if k not in t:
                raise PlanParseError(f"task missing '{k}'")
        if not isinstance(t["depends_on"], list):
            raise PlanParseError("depends_on must be a list")
        try:
            float(t["est_effort_hours"])
        except (TypeError, ValueError):
            raise PlanParseError("est_effort_hours must be numeric")
        if t["temp_id"] in seen:
            raise PlanParseError(f"duplicate temp_id {t['temp_id']!r}")
        seen.add(t["temp_id"])
    for t in tasks:
        for d in t["depends_on"]:
            if d not in seen:
                raise PlanParseError(f"depends_on references unknown temp_id {d!r}")
    return tasks


class EchoProvider:
    """Deterministic. Turn 1 (a user turn, tools available): call the first tool with the
    user's text. Turn 2 (after a tool result): finalize by echoing the tool output.
    With no tools: finalize immediately. Fully reproducible.
    """

    def complete(
        self, *, system: str, messages: list[dict], tools: list[dict], max_tokens: int
    ) -> Completion:
        last = messages[-1]
        in_tokens = sum(_tok(str(m.get("content", ""))) for m in messages)
        if last["role"] == "user" and tools:
            text = str(last["content"])
            return Completion(
                text=None,
                tool_calls=[
                    ToolCall(id="call_1", name=tools[0]["name"], args={"text": text})
                ],
                input_tokens=in_tokens,
                output_tokens=_tok(text),
                stop_reason="tool_use",
            )
        # finalize — deterministically reflect any RULE: directives from the system prompt so the
        # active Playbook is observably applied without a real model (a real model uses them directly).
        out = f"echo: {last.get('content')}"
        rules = [
            ln.split("RULE:", 1)[1].strip()
            for ln in (system or "").splitlines()
            if "RULE:" in ln
        ]
        if rules:
            out += "\nSOP applied: " + "; ".join(rules)
        return Completion(
            text=out, input_tokens=in_tokens, output_tokens=_tok(out), stop_reason="end"
        )

    def plan(self, *, goal: str, departments: list[str], max_tokens: int) -> PlanResult:
        # deterministic demo DAG; departments not covered by the plan fall back to Development
        return PlanResult(
            tasks=[dict(t) for t in _ECHO_PLAN],
            input_tokens=_tok(goal),
            output_tokens=40,
        )


def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Anthropic's Messages API has no "tool" role — a tool result must be a "user" message
    carrying a tool_result content block, matched back to the assistant's tool_use block by id.
    runs.py's internal message list is provider-agnostic (role="tool", flat string content) so it
    can also feed Ollama-family providers, which DO accept that shape loosely — Anthropic needs
    real translation or every second turn of any tool-using run 400s. Messages runs.py never
    tags (plain user/assistant text) pass through unchanged."""
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args}
                for tc in m["tool_calls"]
            ]})
        elif m["role"] == "tool":
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""), "content": m["content"]}
            ]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


class AnthropicProvider:
    """Real provider. Off the Phase 0 gate path — exercised only with a key present."""

    def __init__(
        self, api_key: str, model: str, timeout: float = 60.0, max_plan_retries: int = 2
    ):
        self.model = model
        self.max_plan_retries = max_plan_retries
        try:
            import anthropic
        except ImportError as e:  # fail closed with a clear message
            raise RuntimeError(
                "anthropic package not installed; `pip install anthropic`"
            ) from e
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=timeout
        )  # bounded: never hangs

    def complete(
        self, *, system: str, messages: list[dict], tools: list[dict], max_tokens: int
    ) -> Completion:
        api_tools = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t["input_schema"],
            }
            for t in tools
        ]
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system or "",
            messages=_to_anthropic_messages(messages),
            tools=api_tools or None,
        )
        text_parts, tool_calls = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, args=dict(block.input))
                )
        return Completion(
            text="".join(text_parts) or None,
            tool_calls=tool_calls,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            stop_reason="tool_use" if tool_calls else "end",
        )

    def plan(self, *, goal: str, departments: list[str], max_tokens: int) -> PlanResult:
        import json

        prompt = (
            f"Decompose this goal into a task DAG. Goal: {goal}\n"
            f"Available departments: {', '.join(departments)}.\n"
            "Return ONLY JSON matching this schema (temp_id referenced by later tasks' depends_on):\n"
            f"{json.dumps(_PLAN_SCHEMA)}\n"
            "Planning rules: order tasks so information flows through dependencies — each task must "
            "build on earlier ones' output, never duplicate them. Route each task to the ONE "
            "best-fit department for its nature of work. Make every acceptance_criteria verifiable "
            "(a reviewer can check yes/no) and specific to this goal, not generic. Keep "
            "est_effort_hours realistic for a specialist."
        )
        messages = [{"role": "user", "content": prompt}]
        last_err: Exception | None = None
        for _ in range(
            self.max_plan_retries + 1
        ):  # validate-and-retry: bad JSON gets one correction pass
            resp = self._client.messages.create(
                model=self.model, max_tokens=max_tokens, messages=messages
            )
            raw = "".join(b.text for b in resp.content if b.type == "text")
            try:
                tasks = validate_plan(json.loads(raw))
                return PlanResult(
                    tasks=tasks,
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                )
            except (json.JSONDecodeError, PlanParseError) as e:
                last_err = e
                messages += [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": f"That was invalid ({e}). Return ONLY valid JSON matching the schema.",
                    },
                ]
        raise PlanParseError(
            f"plan invalid after {self.max_plan_retries + 1} attempts: {last_err}"
        )  # fail closed


class OllamaProvider:
    """Local models via Ollama's OpenAI-compatible endpoint. No key, no cost. Text-only (workers
    produce artifacts as text); JSON calls (plan/critic) are extracted + validated with retry."""

    def __init__(
        self,
        model: str,
        base_url: str,
        # a big local model on modest hardware is genuinely slow — observed live: a single trivial
        # completion took ~70s on this machine, and a real multi-turn task (worker + revise cycles)
        # can exceed 300s on one call alone. Still bounded, just not so tight it turns real, slow-but-
        # working inference into a manufactured "model call: timed out" failure.
        timeout: float = 900.0,
        max_plan_retries: int = 2,
        api_key: str | None = None,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_plan_retries = max_plan_retries
        self.api_key = api_key

    def _chat(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int,
        json_mode: bool = False,
        tools: list[dict] | None = None,
    ):
        import json

        import httpx

        msgs = [{"role": "system", "content": system}] if system else []
        for m in messages:
            role = (
                m["role"] if m["role"] in ("user", "assistant", "system") else "user"
            )  # tool->user
            msgs.append({"role": role, "content": str(m["content"])})
        payload = {
            "model": self.model,
            "messages": msgs,
            "stream": False,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            # our internal tool shape (name/description/input_schema) -> OpenAI's function-calling shape,
            # which every OpenAI-compatible endpoint (Mistral, OpenRouter, Ollama) actually speaks
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ]
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        r = httpx.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            timeout=self.timeout,
            headers=headers,
        )
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        usage = data.get("usage", {})
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {}) or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}  # a model that emits malformed JSON args gets an empty-args call, not a crash
            tool_calls.append(
                ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), args=args)
            )
        return (
            content,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            tool_calls,
        )

    def complete(
        self, *, system: str, messages: list[dict], tools: list[dict], max_tokens: int
    ) -> Completion:
        content, inp, out, tool_calls = self._chat(
            system, messages, max_tokens, tools=tools
        )
        if not tool_calls and content and tools:
            content = strip_narrated_tool_calls(content)
        stop_reason = "tool_use" if tool_calls else "end"
        return Completion(
            text=content or None,
            tool_calls=tool_calls,
            input_tokens=inp,
            output_tokens=out,
            stop_reason=stop_reason,
        )

    def plan(self, *, goal: str, departments: list[str], max_tokens: int) -> PlanResult:
        import json

        prompt = (
            f"Decompose this goal into a task DAG. Goal: {goal}\n"
            f"Available departments: {', '.join(departments)}.\n"
            "Return ONLY a JSON object with a 'tasks' array. Each task: temp_id, goal, "
            "acceptance_criteria, department (from the list), est_effort_hours (number), "
            "depends_on (array of earlier temp_ids). No prose.\n"
            "Planning rules: order tasks so information flows through dependencies — each task must "
            "build on earlier ones' output, never duplicate them. Route each task to the ONE "
            "best-fit department for its nature of work. Make every acceptance_criteria verifiable "
            "(a reviewer can check yes/no) and specific to this goal, not generic. Keep "
            "est_effort_hours realistic for a specialist."
        )
        messages = [{"role": "user", "content": prompt}]
        last_err: Exception | None = None
        for _ in range(self.max_plan_retries + 1):
            content, inp, out, _tc = self._chat(
                "You output only JSON.", messages, max_tokens, json_mode=True
            )
            try:
                tasks = validate_plan(json.loads(extract_json_object(content)))
                return PlanResult(tasks=tasks, input_tokens=inp, output_tokens=out)
            except (json.JSONDecodeError, PlanParseError) as e:
                last_err = e
                messages += [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": f"That was invalid ({e}). Return ONLY the JSON object.",
                    },
                ]
        raise PlanParseError(
            f"plan invalid after {self.max_plan_retries + 1} attempts: {last_err}"
        )


class MistralProvider(OllamaProvider):
    """Mistral cloud via its OpenAI-compatible endpoint — fast, and far better than a local 7B."""

    def __init__(self, api_key: str, model: str, base_url: str, timeout: float = 60.0):
        super().__init__(
            model=model, base_url=base_url, timeout=timeout, api_key=api_key
        )


class OpenRouterProvider(OllamaProvider):
    """OpenRouter — one key, many hosted models behind a single OpenAI-compatible endpoint. Model
    strings look like 'mistralai/mistral-small-3.2-24b-instruct:free' or 'anthropic/claude-3.5-sonnet'
    (see openrouter.ai/models); the "brain" behind an agent is just whatever model string is set."""

    def __init__(self, api_key: str, model: str, base_url: str, timeout: float = 60.0):
        super().__init__(
            model=model, base_url=base_url, timeout=timeout, api_key=api_key
        )


# provider name -> the Settings attribute holding its server-wide (.env) fallback key
_ENV_KEY_ATTR = {
    "anthropic": "anthropic_api_key",
    "mistral": "mistral_api_key",
    "openrouter": "openrouter_api_key",
}


def resolve_api_key(db: Session, org_id: str, provider: str) -> str | None:
    """The key build_provider should use for `provider`: an org-level key configured via the console
    (Governance -> Model) takes priority over the server-wide env var, so a key entered in the UI
    works immediately — no .env edit, no restart. Keys are stored PER PROVIDER (org.llm_api_keys),
    never a single flat field — switching providers must never resurrect a stale key that belonged
    to a different one. Providers needing no key (echo, ollama) always get None."""
    if provider not in _ENV_KEY_ATTR:
        return None
    from app.config import settings
    from app.models import Organization

    org = db.get(Organization, org_id)
    stored = (org.llm_api_keys or {}).get(provider) if org else None
    return stored or getattr(settings, _ENV_KEY_ATTR[provider])


def configure_org_llm(
    db: Session, org_id: str, provider: str, model: str, api_key: str | None
) -> None:
    """Point every agent in the org at `provider`/`model` and store its key under that provider's own
    slot in org.llm_api_keys (the console's 'Model' settings save). A blank api_key keeps whatever
    key that SPECIFIC provider already had (or its env var) — switching providers back and forth
    never loses or cross-applies a key."""
    from app.models import AgentProfile, Organization

    org = db.get(Organization, org_id)
    if org is None:
        raise RuntimeError("org not found")
    org.llm_provider, org.llm_model = provider, model
    if api_key:
        keys = dict(org.llm_api_keys or {})
        keys[provider] = api_key
        org.llm_api_keys = keys
    for prof in db.scalars(select(AgentProfile).where(AgentProfile.org_id == org_id)):
        prof.provider, prof.model = provider, model


def build_provider(provider: str, model: str, api_key: str | None):
    if provider == "echo":
        return EchoProvider()
    if provider == "ollama":
        from app.config import settings
        from app.services import cost

        cost.register_free(
            model
        )  # local models are free; keeps cost.compute fail-closed for paid ones
        return OllamaProvider(model=model, base_url=settings.ollama_base_url)
    if provider in _ENV_KEY_ATTR:
        from app.config import settings

        key = api_key or getattr(settings, _ENV_KEY_ATTR[provider])
        if not key:
            raise RuntimeError(
                f"{provider} provider requires an API key "
                f"(configure it in Governance, or set it in .env)"
            )  # fail closed
        if provider == "mistral":
            return MistralProvider(
                api_key=key, model=model, base_url=settings.mistral_base_url
            )
        if provider == "openrouter":
            from app.services import cost

            # OpenRouter's model catalog is open-ended (any string the user types), so it can never
            # be fully covered by the static RATES table, and cost.compute() fails closed on an
            # unpriced model — which would silently turn a SUCCESSFUL completion into a reported
            # run failure. Register it at $0 so the run completes; this is honestly NOT real pricing
            # (OpenRouter is a paid API) — cost caps do not protect OpenRouter spend until real
            # per-model pricing is wired up here. ponytail: fetch openrouter.ai/api/v1/models pricing.
            cost.register_free(model)
            return OpenRouterProvider(
                api_key=key, model=model, base_url=settings.openrouter_base_url
            )
        return AnthropicProvider(api_key=key, model=model)
    raise RuntimeError(f"unknown provider {provider!r}")
