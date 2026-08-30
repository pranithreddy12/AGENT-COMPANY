"""Per-agent persistent memory: what an agent personally remembers across everything it has ever
worked on, independent of any one project — distinct from the project-scoped shared memory in
planning.py (MemoryRecord scope="project"), which is "what the team knows about THIS project".
Read before an agent answers a chat message or works a task; written after it produces something
worth remembering.

Summarized when it gets long: a model reading a wall of every terse entry it ever wrote would burn
real context for little benefit. Past a size threshold the raw entries get compressed into a short
digest by the agent's own configured model instead of being fed in raw — scale is handled at READ
time by summarization, not by limiting what gets written.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Actor, AgentProfile, MemoryRecord

MEMORY_SCOPE = "agent"
_RAW_CAP = 1200        # below this, the raw recent entries are shown as-is — no summarization needed
_MAX_ENTRIES = 40      # never read more rows than this, however far back an agent's history goes
_SUMMARY_MAX_TOKENS = 220


def remember_agent(db: Session, org_id: str, actor_id: str, content: str,
                   project_id: str | None = None) -> None:
    """Append one line to an agent's own memory — what it did or concluded, worth carrying into its
    next task or answer. Callers keep each entry terse (one line); scale is handled at read time."""
    db.add(MemoryRecord(org_id=org_id, scope=MEMORY_SCOPE, project_id=project_id,
                        source_actor_id=actor_id, content=content.strip()[:400]))
    db.flush()


def _raw_entries(db: Session, actor_id: str) -> list[str]:
    rows = db.scalars(
        select(MemoryRecord)
        .where(MemoryRecord.scope == MEMORY_SCOPE, MemoryRecord.source_actor_id == actor_id)
        .order_by(MemoryRecord.created_at.desc())
        .limit(_MAX_ENTRIES)
    )
    return [r.content for r in rows][::-1]  # oldest first — reads like a timeline, not a jumble


def agent_memory_context(db: Session, org_id: str, actor: Actor) -> str:
    """What this agent should read about its own past before answering or working. Empty string if
    it has no memory yet. Summarizes via the agent's own model once the raw log is long enough that
    feeding it in full would cost real context for little benefit; falls back to the plain recent
    entries (Echo has no real reasoning to summarize with, and any summarization error) — a memory
    read must never block or crash the actual work it's in service of."""
    entries = _raw_entries(db, actor.id)
    if not entries:
        return ""
    raw = "\n".join(f"- {e}" for e in entries)
    if len(raw) <= _RAW_CAP:
        return raw

    prof = db.get(AgentProfile, actor.agent_profile_id) if actor.agent_profile_id else None
    if prof is None or prof.provider == "echo":
        return raw[-_RAW_CAP:]  # Echo can't summarize — keep the most recent slice instead of nothing

    from app.services import llm
    try:
        provider = llm.build_provider(prof.provider, prof.model, llm.resolve_api_key(db, org_id, prof.provider))
        system = (
            "Summarize this agent's own work history into a short digest (5-8 bullet points): what "
            "it has done, decisions it made, anything it should remember going forward. Keep concrete "
            "specifics (names, numbers, decisions) - drop routine detail."
        )
        comp = provider.complete(system=system, messages=[{"role": "user", "content": raw}],
                                 tools=[], max_tokens=_SUMMARY_MAX_TOKENS)
        summary = (comp.text or "").strip()
        return summary or raw[-_RAW_CAP:]
    except Exception:
        return raw[-_RAW_CAP:]
