"""Tool registry + builtin implementations. Agents reach tools only via grants — never
by importing them. Each tool declares a side-effect class (read|write|irreversible).
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ToolRegistration


def _web_search(args: dict) -> dict:
    """Real web search (Serper) any agent can call mid-task — not just Rex's one-time project-start
    research. Raises if SERPER_API_KEY isn't configured; runs.execute() catches that and fails the
    run cleanly with the reason, same as any other tool error."""
    from app.services.research import serper_search
    query = (args or {}).get("query", "").strip()
    if not query:
        return {"error": "query is required", "results": []}
    num = max(1, min(int(args.get("num") or 5), 10))  # cap: a model can't ask for hundreds of results
    return {"results": serper_search(query, num=num)}


# Builtin implementations keyed by name. Phase 0 ships read-only tools only.
# Each impl: (fn(args)->dict, side_effect, input_schema, description).
BUILTINS: dict[str, dict] = {
    "echo": {
        "fn": lambda args: {"text": args.get("text", "")},
        "side_effect": "read",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "description": "Return the text passed in. Trivial read tool for demos/tests.",
    },
    "get_time": {
        "fn": lambda args: {"utc": datetime.now(timezone.utc).isoformat()},
        "side_effect": "read",
        "input_schema": {"type": "object", "properties": {}},
        "description": "Return current UTC time.",
    },
    "web_search": {
        "fn": _web_search,
        "side_effect": "read",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "what to search for"},
            "num": {"type": "integer", "description": "number of results, 1-10 (default 5)"}},
            "required": ["query"]},
        "description": ("Search the live web (via Serper) and get back titles, links, and snippets. "
                        "Use this whenever you need a current fact, news, a competitor, pricing, or "
                        "anything else you don't already know for certain — don't guess or make it up."),
    },
}


class ToolError(Exception):
    pass


def register_builtins(db: Session, org_id: str, names: list[str]) -> list[ToolRegistration]:
    regs = []
    for name in names:
        spec = BUILTINS[name]
        reg = ToolRegistration(
            org_id=org_id,
            name=name,
            description=spec["description"],
            input_schema=spec["input_schema"],
            side_effect=spec["side_effect"],
            cost_estimate_usd=0.0,
        )
        db.add(reg)
        regs.append(reg)
    return regs


def granted_tools(db: Session, org_id: str, grants: list[str]) -> list[ToolRegistration]:
    if not grants:
        return []
    return list(
        db.scalars(
            select(ToolRegistration).where(
                ToolRegistration.org_id == org_id, ToolRegistration.name.in_(grants)
            )
        )
    )


def execute(db: Session, org_id: str, grants: list[str], name: str, args: dict) -> dict:
    if name not in grants:
        raise ToolError(f"tool {name!r} not granted to this agent")  # fail closed
    reg = db.scalars(
        select(ToolRegistration).where(
            ToolRegistration.org_id == org_id, ToolRegistration.name == name
        )
    ).first()
    if reg is None:
        raise ToolError(f"tool {name!r} not registered for org")
    if name not in BUILTINS:
        raise ToolError(f"no implementation for tool {name!r}")
    return BUILTINS[name]["fn"](args or {})
