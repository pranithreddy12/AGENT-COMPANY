"""Tool registry + builtin implementations. Agents reach tools only via grants — never
by importing them. Each tool declares a side-effect class (read|write|irreversible).
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ToolRegistration

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
