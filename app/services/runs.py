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
from app.services import cost, events, tools
from app.services.llm import Completion, build_provider, resolve_api_key


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


def execute(db: Session, run: AgentRun, extra_system: str = "") -> AgentRun:
    """extra_system is composed into the model's system prompt for this run — this is how the
    active Playbook reaches the agent (real in-context SOP loading, not post-hoc string edits)."""
    actor = db.get(Actor, run.actor_id)
    profile = db.get(AgentProfile, actor.agent_profile_id) if actor.agent_profile_id else None
    if profile is None:
        return _finish(db, run, "failed", error="actor has no agent profile")

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
        if r.name != "echo" or profile.provider == "echo"
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

        try:
            t0 = time.monotonic()
            comp: Completion = provider.complete(
                system=system, messages=messages,
                tools=tool_specs, max_tokens=profile.max_tokens,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            step_cost = cost.compute(profile.model, comp.input_tokens, comp.output_tokens)
        except Exception as e:  # provider or cost failure -> stop
            return _finish(db, run, "failed", error=f"model call: {e}")

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
            return _finish(db, run, "succeeded", result={"text": comp.text})

        # execute tool calls, feed results back. tool_calls carries the raw ToolCalls (id/name/args)
        # alongside the placeholder text — Anthropic needs the real tool_use blocks (with matching
        # ids) to build a valid follow-up turn; providers that don't care just ignore the extra key.
        messages.append({
            "role": "assistant", "content": f"[tool_use {[tc.name for tc in comp.tool_calls]}]",
            "tool_calls": comp.tool_calls,
        })
        for tc in comp.tool_calls:
            try:
                result = tools.execute(db, run.org_id, grants, tc.name, tc.args)
            except Exception as e:  # ungranted / unknown tool -> stop
                return _finish(db, run, "failed", error=f"tool {tc.name}: {e}")
            events.append(
                db, org_id=run.org_id, trace_id=run.trace_id, run_id=run.id, actor_id=run.actor_id,
                action="tool.call", target=tc.name, before={"args": tc.args}, after={"result": result},
            )
            # json.dumps, not str(): a Python dict repr ({'a': 'b'}) isn't valid JSON and reads as
            # ambiguous noise to a model, especially a weaker local one — this labels it clearly and
            # gives back parseable data instead of a data structure impersonating a user message.
            # tool_call_id lets Anthropic match this result back to the tool_use block that asked for it.
            messages.append({
                "role": "tool", "content": f"Tool '{tc.name}' result: {json.dumps(result)}",
                "tool_call_id": tc.id,
            })

    return _finish(db, run, "failed", error="max_turns exhausted")
