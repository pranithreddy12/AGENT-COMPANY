"""Keyed validation run — the actual test of the intelligence layer.

Prereqs:
    pip install anthropic
    export ANTHROPIC_API_KEY=sk-...            (Windows: set ANTHROPIC_API_KEY=...)
    python -m scripts.eval_live                (optionally EVAL_MODEL=claude-opus-4-8)

Runs the SAME code paths the platform uses, but with every agent pointed at a real Anthropic
model. Scores three things that no green test proves today:
  1. lead_decomposition — the Lead turns a novel goal into a sane, acyclic, multi-step DAG.
  2. sop_behavior       — editing the Playbook (not the prompt) changes agent output. THE claim.
  3. critic             — the Critic passes a good artifact and rejects an empty one.
"""
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register mappers
from app.config import settings
from app.db import Base
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import evals


def main() -> int:
    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set — nothing to validate. See the module docstring.")
        return 1
    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("`pip install anthropic` first.")
        return 1

    model = os.environ.get("EVAL_MODEL", "claude-sonnet-5")
    engine = create_engine("sqlite:///eval_live.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    org_id = create_org(OrgCreate(name="EvalCo", ceo_email="e@e.com", ceo_password="pw"), db).org_id
    evals.use_anthropic(db, org_id, model=model)
    print(f"Running live evals against {model} ...\n")

    out = evals.run_all(db, org_id)
    for r in out["results"]:
        mark = "PASS" if r["passed"] else "FAIL"
        extra = {k: v for k, v in r.items() if k not in ("name", "passed")}
        print(f"[{mark}] {r['name']}: {extra}")
    print(f"\n{out['passed']}/{out['total']} evals passed against the real model.")
    return 0 if out["passed"] == out["total"] else 2


if __name__ == "__main__":
    sys.exit(main())
