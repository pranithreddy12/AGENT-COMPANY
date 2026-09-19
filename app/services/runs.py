"""Agent run state machine + bounded executor.

States: queued -> running -> succeeded | failed | killed.
Every turn writes Events. Bounded by max_turns / cost_ceiling / max_tokens. Killable
between turns. Fails closed on any provider/tool/validation error.

# ponytail: synchronous executor. State lives in DB rows, so dropping this behind a
# Celery queue later is a local change — the FSM and events don't move.
"""
import json
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Actor, AgentProfile, AgentRun
from app.config import settings
from app.services import cost, events, governance, llm, tools
from app.services.llm import Completion, build_provider, complete_with_retry, explain_error, resolve_api_key


class RunError(Exception):
    pass


def create_run(db: Session, org_id: str, actor: Actor, trigger: str) -> AgentRun:
    run = AgentRun(org_id=org_id, actor_id=actor.id, trigger=trigger, status="queued")
    db.add(run)
    db.flush()  # assigns id + trace_id
    return run


def _finish(db: Session, run: AgentRun, status: str, *, result=None, error=None):
    run.status = status
    run.result = result
    run.error = error
    run.ended_at = datetime.now(timezone.utc)
    events.append(
        db, org_id=run.org_id, trace_id=run.trace_id, run_id=run.id, actor_id=run.actor_id,
        action=f"run.{status}", after={"result": result, "error": error},
    )
    db.commit()
    return run


class MeteredProvider:
    """Wraps a provider so a model call made OUTSIDE runs.execute — the Critic, chat replies, intent
    classification, memory summaries, research, proposals — is still governed and counted.

    Before this, only task runs wrote cost anywhere, so the org's "Spent $X of $100 cap" ignored every
    one of those calls (several per task), and the kill switch / budget cap didn't stop them either.
    Each call checks governance first (raises governance.Blocked) and records an AgentRun row whose
    cost_usd feeds governance.spent(). Callers wrap this in llm.complete_with_retry for retries."""

    def __init__(self, provider, db: Session, org_id: str, actor_id: str | None, model: str, label: str,
                 department_id: str | None = None):
        self._p, self._db, self._org, self._actor = provider, db, org_id, actor_id
        self._model, self._label, self._dept = model, label, department_id

    def complete(self, **kwargs) -> Completion:
        reason = governance.run_block_reason(self._db, self._org, self._dept)
        if reason:
            raise governance.Blocked(reason)
        comp = self._p.complete(**kwargs)
        # accounting must never break the call it is accounting for
        step = cost.compute_or_default(self._model, getattr(comp, "input_tokens", 0) or 0,
                                       getattr(comp, "output_tokens", 0) or 0)
        now = datetime.now(timezone.utc)
        if self._actor:  # AgentRun.actor_id is NOT NULL — an actor-less call can't be recorded
            self._db.add(AgentRun(
                org_id=self._org, actor_id=self._actor, trigger=self._label, status="succeeded",
                turns_used=1, cost_usd=step, started_at=now, ended_at=now,
            ))
            self._db.flush()
        return comp


def metered(db: Session, org_id: str, actor: Actor | None, profile: AgentProfile, label: str) -> MeteredProvider:
    """Build the provider for `profile` (its own key/model resolution) wrapped for governance + cost."""
    # via the llm module (not the names imported above) so patching llm.build_provider reaches this too
    provider = llm.build_provider(profile.provider, profile.model, llm.resolve_api_key(db, org_id, profile.provider))
    return MeteredProvider(provider, db, org_id, actor.id if actor else None, profile.model, label,
                           actor.department_id if actor else None)


def execute(db: Session, run: AgentRun, extra_system: str = "") -> AgentRun:
    """extra_system is composed into the model's system prompt for this run — this is how the
    active Playbook reaches the agent (real in-context SOP loading, not post-hoc string edits)."""
    actor = db.get(Actor, run.actor_id)
    profile = db.get(AgentProfile, actor.agent_profile_id) if actor.agent_profile_id else None
    if profile is None:
        return _finish(db, run, "failed", error="actor has no agent profile")

    blocked = governance.run_block_reason(db, run.org_id, actor.department_id)
    if blocked:  # kill switch / paused department / budget cap — checked BEFORE any model spend
        return _finish(db, run, "failed", error=f"blocked: {blocked}")

    try:
        provider = build_provider(profile.provider, profile.model, resolve_api_key(db, run.org_id, profile.provider))
    except Exception as e:  # fail closed
        return _finish(db, run, "failed", error=f"provider init: {e}")

    grants = profile.tool_grants or []
    # "echo" only exists so EchoProvider's deterministic finalize step has a tool to round-trip
    # through — a real model has no legitimate reason to call a tool that just returns what it's
    # given, and offering it as an option has produced garbled replies where the model calls it
    # with its whole answer instead of just answering. Never advertise it outside Echo mode.
    tool_specs = [
        {"name": r.name, "description": r.description, "input_schema": r.input_schema}
        for r in tools.granted_tools(db, run.org_id, grants)
        if (r.name != "echo" or profile.provider == "echo")
        # a tool that can only ever error just teaches the model to waste a turn on it
        and (r.name != "web_search" or settings.serper_api_key)
    ]

    run.status = "running"
    run.started_at = datetime.now(timezone.utc)
    events.append(
        db, org_id=run.org_id, trace_id=run.trace_id, run_id=run.id, actor_id=run.actor_id,
        action="run.started", after={"trigger": run.trigger},
    )
    db.flush()

    system = (profile.system_prompt + "\n\n" + extra_system).strip() if extra_system else profile.system_prompt
    messages: list[dict] = [{"role": "user", "content": run.trigger}]

    for turn in range(profile.max_turns):
        db.refresh(run)
        if run.kill_requested:
            return _finish(db, run, "killed", error="kill requested")
        if turn > 0:  # the kill switch / budget can flip mid-run — don't keep spending through it
            blocked = governance.run_block_reason(db, run.org_id, actor.department_id)
            if blocked:
                return _finish(db, run, "failed", error=f"blocked: {blocked}")

        # last allowed turn: take the tools away and ask for the answer, so a model that keeps
        # reaching for tools ends with a deliverable instead of "max_turns exhausted" and no output.
        # Not for Echo — its deterministic two-turn dance must stay exactly as tested.
        last_turn = turn == profile.max_turns - 1 and profile.provider != "echo"
        if last_turn and any(m["role"] == "tool" for m in messages):
            messages.append({"role": "user", "content":
                             "Tool use is finished. Write your final deliverable now, using what you have."})

        try:
            t0 = time.monotonic()
            comp: Completion = complete_with_retry(
                provider, system=system, messages=messages,
                tools=[] if last_turn else tool_specs, max_tokens=profile.max_tokens,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            step_cost = cost.compute_or_default(profile.model, comp.input_tokens, comp.output_tokens)
        except Exception as e:  # provider or cost failure -> stop
            return _finish(db, run, "failed", error=f"model call: {explain_error(e)}")

        run.turns_used = turn + 1
        run.cost_usd = round(run.cost_usd + step_cost, 6)
        events.append(
            db, org_id=run.org_id, trace_id=run.trace_id, run_id=run.id, actor_id=run.actor_id,
            action="model.call", target=profile.model, cost_usd=step_cost, latency_ms=latency_ms,
            before={"tokens_in": comp.input_tokens},
            after={"tokens_out": comp.output_tokens, "stop_reason": comp.stop_reason},
        )

        if run.cost_usd > profile.cost_ceiling_usd:
            return _finish(db, run, "failed", error=f"cost_ceiling exceeded (${run.cost_usd})")

        if comp.stop_reason != "tool_use":
            if not (comp.text or "").strip():
                # an empty reply is not a deliverable — and would crash the NOT NULL artifact write
                return _finish(db, run, "failed", error="model returned an empty response")
            return _finish(db, run, "succeeded", result={"text": comp.text})

        # execute tool calls, feed results back. tool_calls carries the raw ToolCalls (id/name/args)
        # alongside the placeholder text — Anthropic needs the real tool_use blocks (with matching
        # ids) to build a valid follow-up turn; providers that don't care just ignore the extra key.
        messages.append({
            "role": "assistant", "content": f"[tool_use {[tc.name for tc in comp.tool_calls]}]",
            "tool_calls": comp.tool_calls,
        })
        for tc in comp.tool_calls:
            failed = False
            try:
                result = tools.execute(db, run.org_id, grants, tc.name, tc.args)
            except Exception as e:
                # a tool failing (no API key, bad args, ungranted) is information for the model, not
                # a reason to kill the whole run: hand the error back so it can carry on without the
                # tool. Failing the run here turned "web search unavailable" into a lost task.
                failed, result = True, {"error": f"{type(e).__name__}: {e}"}
            events.append(
                db, org_id=run.org_id, trace_id=run.trace_id, run_id=run.id, actor_id=run.actor_id,
                action="tool.call", target=tc.name, before={"args": tc.args},
                after={"result": result, "failed": failed},
            )
            # json.dumps, not str(): a Python dict repr ({'a': 'b'}) isn't valid JSON and reads as
            # ambiguous noise to a model, especially a weaker local one — this labels it clearly and
            # gives back parseable data instead of a data structure impersonating a user message.
            # tool_call_id lets Anthropic match this result back to the tool_use block that asked for it.
            label = f"Tool '{tc.name}' FAILED (continue without it): " if failed else f"Tool '{tc.name}' result: "
            messages.append({
                "role": "tool", "content": f"{label}{json.dumps(result)}",
                "tool_call_id": tc.id,
            })

    return _finish(db, run, "failed", error="max_turns exhausted")
