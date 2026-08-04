"""Research agent: real web search (Serper) at the start of a project. Rex searches the goal,
synthesizes a sourced brief, and writes it into shared memory so every other agent builds on real
research instead of guessing.
"""
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Actor, AgentProfile, MemoryRecord, Project
from app.services import llm

_RESEARCH_SYSTEM = (
    "You are the Research agent at an agency. Turn the web search results into a concise, factual "
    "research brief for your team: key facts, notable competitors/examples, best practices, and "
    "concrete specifics relevant to the goal. Cite sources inline as (source: domain). No fluff."
)


def serper_search(query: str, num: int = 6) -> list[dict]:
    if not settings.serper_api_key:
        raise RuntimeError("SERPER_API_KEY not set")
    r = httpx.post(
        f"{settings.serper_base_url}/search",
        headers={"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"},
        json={"q": query, "num": num}, timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return [{"title": o.get("title", ""), "link": o.get("link", ""), "snippet": o.get("snippet", "")}
            for o in data.get("organic", [])[:num]]


def rex(db: Session, org_id: str) -> Actor | None:
    return db.scalars(select(Actor).where(Actor.org_id == org_id, Actor.role == "research")).first()


_BRIEF_CONTEXT_CAP = 1500  # chars of the brief injected into every agent's context (bounds token cost)


def run_research(db: Session, project: Project) -> str | None:
    """Search the web on the goal, synthesize a brief, write it to shared memory. Returns a one-line
    summary for the team chat, or None if research is unavailable/failed — research is always optional
    and must never sink the project run."""
    agent = rex(db, project.org_id)
    if agent is None or not settings.serper_api_key:
        return None
    # already researched this project? don't run a second billed search / duplicate the brief on re-run
    if db.scalars(select(MemoryRecord).where(
            MemoryRecord.project_id == project.id, MemoryRecord.source_actor_id == agent.id)).first():
        return None

    try:  # ANY research failure (search, provider init, or synthesis) is swallowed — never aborts execute
        results = serper_search(project.goal, num=6)
        if not results:
            return None
        sources = "\n".join(f"- {r['title']}: {r['snippet']} ({r['link']})" for r in results)
        prof = db.get(AgentProfile, agent.agent_profile_id)
        provider = llm.build_provider(prof.provider, prof.model, settings.anthropic_api_key)
        comp = provider.complete(
            system=_RESEARCH_SYSTEM,
            messages=[{"role": "user", "content": f"Goal: {project.goal}\n\nWeb results:\n{sources}\n\nWrite the brief."}],
            tools=[], max_tokens=1024,  # a concise brief; the whole team re-reads it, so keep it tight
        )
    except Exception:
        return None
    brief = (comp.text or "").strip()
    if not brief:
        return None

    # cap what goes into shared context so it isn't re-sent in full on every worker/critic call
    snippet = brief[:_BRIEF_CONTEXT_CAP] + (" …[brief truncated]" if len(brief) > _BRIEF_CONTEXT_CAP else "")
    db.add(MemoryRecord(org_id=project.org_id, scope="project", project_id=project.id,
                        source_actor_id=agent.id,
                        content=f"WEB RESEARCH (Rex Research Agent) on '{project.goal}':\n{snippet}"))
    db.flush()
    return f"web research done — reviewed {len(results)} sources, brief shared with the team."
