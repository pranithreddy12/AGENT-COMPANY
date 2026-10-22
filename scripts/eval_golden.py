"""Golden-task eval: run realistic agency tasks through the REAL pipeline with a real model and report
what a user would feel - how often a first draft is clean, how often a human is still needed, what it
costs, how long it takes. Runs on a throwaway in-memory org; your database is never touched.

    python -m scripts.eval_golden                          # mistral / mistral-small-latest (needs MISTRAL_API_KEY)
    EVAL_PROVIDER=ollama EVAL_MODEL=qwen2.5:7b python -m scripts.eval_golden
    EVAL_TASKS=2 python -m scripts.eval_golden             # first N tasks only (cheap smoke)

It exists so a model, prompt or validator change is *measured* (invalid-output rate, first-pass rate,
cost, latency) instead of felt. Add tasks you actually care about to GOLDEN below.
"""
import json
import os
import sys
import time

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.db import Base
from app.models import Actor, AgentRun, Organization, Project, Task
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import governance, llm, planning

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROFILE = ("Acme Dental Marketing is a 5-person agency in Dubai. We run Instagram-led lead generation for "
           "private dental clinics. Tone: warm, direct, plain English, no hype.")
PRICES = "Starter: $1,200/month\nGrowth: $2,500/month\nOne-off Instagram audit: $900"

# (agent name, task) - realistic day-to-day agency work, including ones that tempt placeholders,
# invented prices and wrong dates.
GOLDEN = [
    ("Sam Sales Agent", "Write a 120-word cold outreach email to a Dubai dental clinic offering our Starter package."),
    ("Mia Marketing Agent", "Draft 3 Instagram captions for a teeth-whitening promotion for a client clinic."),
    ("Piper Planning Agent", "Create a 2-week kickoff schedule for onboarding a new client, with calendar dates starting next Monday."),
    ("Cleo Client Agent", "Write a short status update email to a client whose Instagram campaign is one week in."),
    ("Sam Sales Agent", "List our three packages with their prices in a short comparison for a prospect."),
    ("Devin Dev Agent", "Outline the steps to connect an Instagram lead-capture form to a simple CRM."),
]


def main() -> None:
    provider = os.environ.get("EVAL_PROVIDER", "mistral")
    model = os.environ.get("EVAL_MODEL", "mistral-small-latest")
    n = int(os.environ.get("EVAL_TASKS", len(GOLDEN)))

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    org_id = create_org(OrgCreate(name="Acme Dental Marketing", ceo_email="e@a.com", ceo_password="pw"), db).org_id
    org = db.get(Organization, org_id)
    org.profile, org.price_list = PROFILE, PRICES
    llm.configure_org_llm(db, org_id, provider, model, None)
    db.commit()

    # count first-draft validator failures (the number that says whether grounding works)
    stats = {"drafts": 0, "draft_issues": 0}
    real = planning._draft_issues

    def counting(db_, org_, actor, content):
        out = real(db_, org_, actor, content)
        stats["drafts"] += 1
        stats["draft_issues"] += bool(out)
        return out

    planning._draft_issues = counting

    project = Project(org_id=org_id, goal="Golden eval", status="active", health="on_track")
    db.add(project)
    db.flush()
    rows = []
    for agent_name, goal in GOLDEN[:n]:
        agent = db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.name == agent_name)).first()
        task = Task(org_id=org_id, project_id=project.id, goal=goal, department_id=agent.department_id,
                    assignee_actor_id=agent.id, status="in_progress", est_effort_hours=1.0)
        db.add(task)
        db.commit()
        before_cost, t0, d0 = governance.spent(db, org_id), time.monotonic(), stats["draft_issues"]
        try:
            art = planning.rerun_task(db, project, task)
            err = None
        except Exception as e:  # a crash is a result too
            art, err = None, f"{type(e).__name__}: {e}"
        rows.append({
            "agent": agent_name, "task": goal[:70], "seconds": round(time.monotonic() - t0, 1),
            "cost_usd": round(governance.spent(db, org_id) - before_cost, 5),
            "first_draft_clean": stats["draft_issues"] == d0,
            "needs_human": bool(art and art.needs_human), "blocked": bool(art and art.blocked),
            "revisions": (art.version - 1) if art else None,
            "reasons": (art.critic_reasons if art else None) or ([err] if err else []),
            "preview": ((art.content or "")[:240].replace("\n", " ") if art else ""),
        })
        print(f"  {'OK ' if art and not art.needs_human else 'FLAG'} {agent_name:22} {rows[-1]['seconds']:>5}s "
              f"${rows[-1]['cost_usd']:<8} {goal[:50]}")

    ok = [r for r in rows if not r["needs_human"] and not r["blocked"]]
    summary = {
        "model": f"{provider}/{model}", "tasks": len(rows),
        "first_draft_clean_rate": round(sum(r["first_draft_clean"] for r in rows) / len(rows), 2),
        "approved_without_human_rate": round(len(ok) / len(rows), 2),
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "avg_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 1),
        "validator_flagged_drafts": f"{stats['draft_issues']}/{stats['drafts']}",
    }
    print("\n" + json.dumps(summary, indent=2))
    out = os.path.join(os.path.dirname(__file__), "..", "eval_golden_last.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "rows": rows}, f, indent=2)
    print("details ->", os.path.abspath(out))


if __name__ == "__main__":
    main()
