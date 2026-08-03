"""Validate the intelligence layer against a LOCAL model via Ollama. No key, no cost.

    ollama serve            # (usually already running)
    ollama pull qwen2.5:7b
    python -m scripts.eval_ollama            # OLLAMA_MODEL=llama3.2:3b to override

Runs the same code paths the platform uses, with every agent pointed at a local model, and scores:
  1. lead_decomposition — the Lead turns a novel goal into a sane, acyclic, multi-step DAG.
  2. sop_behavior       — editing the Playbook (system prompt) changes agent output. THE claim.
  3. critic             — the Critic passes a good artifact and rejects an empty one.
"""
import os
import sys

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.config import settings
from app.db import Base
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import evals


def main() -> int:
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
    try:
        tags = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5).json()
    except Exception as e:
        print(f"Ollama not reachable at {settings.ollama_base_url} ({e}). Run `ollama serve`.")
        return 1
    have = [m["name"] for m in tags.get("models", [])]
    if model not in have:
        print(f"Model {model!r} not installed. Have: {have}. Run `ollama pull {model}`.")
        return 1

    engine = create_engine("sqlite:///eval_ollama.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    org_id = create_org(OrgCreate(name="EvalCo", ceo_email="e@e.com", ceo_password="pw"), db).org_id
    evals.use_ollama(db, org_id, model=model)
    print(f"Running live evals against local model {model} ...\n")

    out = evals.run_all(db, org_id)
    for r in out["results"]:
        mark = "PASS" if r["passed"] else "FAIL"
        extra = {k: v for k, v in r.items() if k not in ("name", "passed")}
        print(f"[{mark}] {r['name']}: {extra}")
    print(f"\n{out['passed']}/{out['total']} evals passed against {model}.")
    return 0 if out["passed"] == out["total"] else 2


if __name__ == "__main__":
    sys.exit(main())
