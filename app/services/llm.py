"""Provider-abstracted LLM layer. Every call is bounded (max_tokens, timeout).

- EchoProvider: deterministic, zero external cost. Powers tests and the Phase 0 demo.
- AnthropicProvider: real. Imported lazily so `anthropic` + a key are optional until used.
"""
from dataclasses import dataclass, field


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
    tasks: list[dict]  # each: temp_id, goal, acceptance_criteria, department, est_effort_hours, depends_on[]
    input_tokens: int = 0
    output_tokens: int = 0


# Deterministic demo decomposition: a cross-department DAG. Spans six departments so cross-team
# handoffs and the critical path are both non-trivial. t_test (Development) carries slack.
_ECHO_PLAN = [
    {"temp_id": "t1", "goal": "Qualify client and confirm scope", "acceptance_criteria": "Scope confirmed with client", "department": "Sales", "est_effort_hours": 2.0, "depends_on": []},
    {"temp_id": "t2", "goal": "Draft technical spec", "acceptance_criteria": "Spec covers confirmed scope", "department": "Development", "est_effort_hours": 5.0, "depends_on": ["t1"]},
    {"temp_id": "t3", "goal": "Review contract and compliance", "acceptance_criteria": "No legal or compliance blockers", "department": "Legal", "est_effort_hours": 3.0, "depends_on": ["t1"]},
    {"temp_id": "t_test", "goal": "Draft test plan", "acceptance_criteria": "Test plan maps to spec", "department": "Development", "est_effort_hours": 2.0, "depends_on": ["t2"]},
    {"temp_id": "t4", "goal": "Draft launch announcement copy", "acceptance_criteria": "Copy on-message, no unverified claims", "department": "Marketing", "est_effort_hours": 2.0, "depends_on": ["t2"]},
    {"temp_id": "t5", "goal": "Finalize delivery schedule", "acceptance_criteria": "Schedule agreed and dated", "department": "Planning", "est_effort_hours": 2.0, "depends_on": ["t2", "t3"]},
    {"temp_id": "t6", "goal": "Compile client status update", "acceptance_criteria": "Update approved for client", "department": "Client Management", "est_effort_hours": 2.0, "depends_on": ["t4", "t5"]},
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
                "required": ["temp_id", "goal", "department", "est_effort_hours", "depends_on"],
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
                return text[start:i + 1]
    return text[start:]


def validate_plan(data: object) -> list[dict]:
    """Validate a model-produced plan against the schema. Pure — no LLM. Fails closed on any
    malformed structure so a bad decomposition never reaches the task materializer."""
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list) or not data["tasks"]:
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

    def complete(self, *, system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> Completion:
        last = messages[-1]
        in_tokens = sum(_tok(str(m.get("content", ""))) for m in messages)
        if last["role"] == "user" and tools:
            text = str(last["content"])
            return Completion(
                text=None,
                tool_calls=[ToolCall(id="call_1", name=tools[0]["name"], args={"text": text})],
                input_tokens=in_tokens,
                output_tokens=_tok(text),
                stop_reason="tool_use",
            )
        # finalize — deterministically reflect any RULE: directives from the system prompt so the
        # active Playbook is observably applied without a real model (a real model uses them directly).
        out = f"echo: {last.get('content')}"
        rules = [ln.split("RULE:", 1)[1].strip() for ln in (system or "").splitlines() if "RULE:" in ln]
        if rules:
            out += "\nSOP applied: " + "; ".join(rules)
        return Completion(text=out, input_tokens=in_tokens, output_tokens=_tok(out), stop_reason="end")

    def plan(self, *, goal: str, departments: list[str], max_tokens: int) -> PlanResult:
        # deterministic demo DAG; departments not covered by the plan fall back to Development
        return PlanResult(tasks=[dict(t) for t in _ECHO_PLAN], input_tokens=_tok(goal), output_tokens=40)


class AnthropicProvider:
    """Real provider. Off the Phase 0 gate path — exercised only with a key present."""

    def __init__(self, api_key: str, model: str, timeout: float = 60.0, max_plan_retries: int = 2):
        self.model = model
        self.max_plan_retries = max_plan_retries
        try:
            import anthropic
        except ImportError as e:  # fail closed with a clear message
            raise RuntimeError("anthropic package not installed; `pip install anthropic`") from e
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout)  # bounded: never hangs

    def complete(self, *, system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> Completion:
        api_tools = [
            {"name": t["name"], "description": t.get("description", ""), "input_schema": t["input_schema"]}
            for t in tools
        ]
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system or "",
            messages=[{"role": m["role"], "content": m["content"]} for m in messages],
            tools=api_tools or None,
        )
        text_parts, tool_calls = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, args=dict(block.input)))
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
            f"{json.dumps(_PLAN_SCHEMA)}"
        )
        messages = [{"role": "user", "content": prompt}]
        last_err: Exception | None = None
        for _ in range(self.max_plan_retries + 1):  # validate-and-retry: bad JSON gets one correction pass
            resp = self._client.messages.create(model=self.model, max_tokens=max_tokens, messages=messages)
            raw = "".join(b.text for b in resp.content if b.type == "text")
            try:
                tasks = validate_plan(json.loads(raw))
                return PlanResult(tasks=tasks, input_tokens=resp.usage.input_tokens,
                                  output_tokens=resp.usage.output_tokens)
            except (json.JSONDecodeError, PlanParseError) as e:
                last_err = e
                messages += [{"role": "assistant", "content": raw},
                             {"role": "user", "content": f"That was invalid ({e}). Return ONLY valid JSON matching the schema."}]
        raise PlanParseError(f"plan invalid after {self.max_plan_retries + 1} attempts: {last_err}")  # fail closed


class OllamaProvider:
    """Local models via Ollama's OpenAI-compatible endpoint. No key, no cost. Text-only (workers
    produce artifacts as text); JSON calls (plan/critic) are extracted + validated with retry."""

    def __init__(self, model: str, base_url: str, timeout: float = 300.0, max_plan_retries: int = 2,
                 api_key: str | None = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_plan_retries = max_plan_retries
        self.api_key = api_key

    def _chat(self, system: str, messages: list[dict], max_tokens: int, json_mode: bool = False):
        import httpx

        msgs = ([{"role": "system", "content": system}] if system else [])
        for m in messages:
            role = m["role"] if m["role"] in ("user", "assistant", "system") else "user"  # tool->user
            msgs.append({"role": role, "content": str(m["content"])})
        payload = {"model": self.model, "messages": msgs, "stream": False, "max_tokens": max_tokens}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        r = httpx.post(f"{self.base_url}/v1/chat/completions", json=payload, timeout=self.timeout, headers=headers)
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage", {})
        return content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)

    def complete(self, *, system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> Completion:
        content, inp, out = self._chat(system, messages, max_tokens)
        return Completion(text=content, tool_calls=[], input_tokens=inp, output_tokens=out, stop_reason="end")

    def plan(self, *, goal: str, departments: list[str], max_tokens: int) -> PlanResult:
        import json

        prompt = (
            f"Decompose this goal into a task DAG. Goal: {goal}\n"
            f"Available departments: {', '.join(departments)}.\n"
            "Return ONLY a JSON object with a 'tasks' array. Each task: temp_id, goal, "
            "acceptance_criteria, department (from the list), est_effort_hours (number), "
            "depends_on (array of earlier temp_ids). No prose."
        )
        messages = [{"role": "user", "content": prompt}]
        last_err: Exception | None = None
        for _ in range(self.max_plan_retries + 1):
            content, inp, out = self._chat("You output only JSON.", messages, max_tokens, json_mode=True)
            try:
                tasks = validate_plan(json.loads(extract_json_object(content)))
                return PlanResult(tasks=tasks, input_tokens=inp, output_tokens=out)
            except (json.JSONDecodeError, PlanParseError) as e:
                last_err = e
                messages += [{"role": "assistant", "content": content},
                             {"role": "user", "content": f"That was invalid ({e}). Return ONLY the JSON object."}]
        raise PlanParseError(f"plan invalid after {self.max_plan_retries + 1} attempts: {last_err}")


class MistralProvider(OllamaProvider):
    """Mistral cloud via its OpenAI-compatible endpoint — fast, and far better than a local 7B."""

    def __init__(self, api_key: str, model: str, base_url: str, timeout: float = 60.0):
        super().__init__(model=model, base_url=base_url, timeout=timeout, api_key=api_key)


def build_provider(provider: str, model: str, api_key: str | None):
    if provider == "echo":
        return EchoProvider()
    if provider == "ollama":
        from app.config import settings
        from app.services import cost
        cost.register_free(model)  # local models are free; keeps cost.compute fail-closed for paid ones
        return OllamaProvider(model=model, base_url=settings.ollama_base_url)
    if provider == "mistral":
        from app.config import settings
        if not settings.mistral_api_key:
            raise RuntimeError("mistral provider requires MISTRAL_API_KEY")  # fail closed
        return MistralProvider(api_key=settings.mistral_api_key, model=model, base_url=settings.mistral_base_url)
    if provider == "anthropic":
        if not api_key:
            raise RuntimeError("anthropic provider requires ANTHROPIC_API_KEY")  # fail closed
        return AnthropicProvider(api_key=api_key, model=model)
    raise RuntimeError(f"unknown provider {provider!r}")
