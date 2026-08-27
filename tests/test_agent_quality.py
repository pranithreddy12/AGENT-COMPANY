"""Agent work quality + interaction: the brief carries the acceptance bar, revisions show the
prior draft, context flows in full, chat agents read the conversation, and handoffs name real work.
"""

from sqlalchemy import select

from app.models import MemoryRecord, Message, Thread
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import planning, teamchat
from app.services.review import Verdict


def _org(db):
    return create_org(
        OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db
    ).org_id


def _run_project(db, org_id):
    project, _ = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    planning.approve_project(db, project)
    planning.execute_project(db, project)
    return project


def _spy_triggers(monkeypatch):
    seen = []
    real = planning.runs.execute

    def spy(db_, run, extra_system=""):
        seen.append(run.trigger)
        return real(db_, run, extra_system=extra_system)

    monkeypatch.setattr(planning.runs, "execute", spy)
    return seen


def _status_messages(db, project):
    thread = db.scalars(
        select(Thread).where(
            Thread.project_id == project.id, Thread.thread_type == "status"
        )
    ).first()
    return list(
        db.scalars(
            select(Message)
            .where(Message.thread_id == thread.id)
            .order_by(Message.created_at)
        )
    )


def test_worker_brief_carries_acceptance_criteria(db, monkeypatch):
    """The Critic judges against task.acceptance_criteria — so the producer must see the same bar."""
    org_id = _org(db)
    project, _ = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    planning.approve_project(db, project)

    seen = _spy_triggers(monkeypatch)
    planning.execute_project(db, project)

    assert any(
        "Acceptance criteria" in t for t in seen
    )  # every producer saw the QA bar


def test_revise_loop_shows_the_agent_its_prior_draft(db, monkeypatch):
    """A rejected draft must be revised, not regenerated blind: attempt 2 carries attempt 1's text
    and every QA reason."""
    org_id = _org(db)
    project, _ = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    planning.approve_project(db, project)

    seen = _spy_triggers(monkeypatch)
    calls = {"n": 0}
    real_review = planning._review

    def flaky_review(db_, org_id_, critic, content, criteria, playbook):
        calls["n"] += 1
        if calls["n"] == 1:
            return Verdict(False, ["no concrete pricing section"])
        return real_review(db_, org_id_, critic, content, criteria, playbook)

    monkeypatch.setattr(planning, "_review", flaky_review)

    planning.execute_project(db, project)

    assert calls["n"] >= 2  # a revise actually happened
    second = seen[1]
    assert "QA rejected your previous draft" in second
    assert "no concrete pricing section" in second  # every QA point is named
    assert "--- Your previous draft ---" in second
    # the prior draft is embedded: its opening line appears twice — once as this run's brief,
    # once inside the quoted previous draft — proving the agent sees its own rejected work
    assert second.count("Your department: Sales.") >= 2


def test_gather_context_keeps_more_and_clips_cleanly(db):
    """Upstream deliverables survive past the old 900-char cap, and over-cap ones clip at a line
    boundary instead of mid-sentence."""

    class T:
        def __init__(self, id, goal, dept, deps):
            self.id, self.goal, self.department_id, self.depends_on = (
                id,
                goal,
                dept,
                deps,
            )

    # 40 lines of 50 chars = 2000 chars: past the old 900 cap, inside the new 2400 cap
    medium_body = ("A" * 49 + "\n") * 40 + "CONCLUSION-MARKER on the final line"
    dep = T("dep1", "Upstream research", "d1", [])
    task = T("t1", "Write the spec", "d2", ["dep1"])
    depts = {"d1": type("D", (), {"name": "Sales"})()}

    def gather(body):
        return planning._gather_context(
            db=None,
            project=None,
            task=task,
            tasks={"dep1": dep},
            artifacts_by_task={"dep1": type("A", (), {"content": body})()},
            depts=depts,
            include_memory=False,
        )

    ctx = gather(medium_body)
    assert (
        "CONCLUSION-MARKER" in ctx
    )  # content past the old 900-char cut now reaches the agent

    huge_body = (
        "Line of analysis with real specifics.\n" * 300
    )  # ~11k chars, well over cap
    clipped = gather(huge_body)
    assert clipped.rstrip().endswith(
        "…[continued]"
    )  # clipped at a line boundary, not mid-word


def test_rerun_task_extra_context_reaches_prompt_and_memory_is_optional(
    db, monkeypatch
):
    org_id = _org(db)
    project, tasks = planning.draft_project(db, org_id, "Deliver a client engagement")
    db.commit()
    planning.approve_project(db, project)
    target = next(t for t in tasks if not t.depends_on)
    db.add(
        MemoryRecord(
            org_id=org_id,
            scope="project",
            project_id=project.id,
            content="STALE-MEMORY-MARKER from an unrelated request",
        )
    )
    db.flush()

    seen = _spy_triggers(monkeypatch)
    planning.rerun_task(
        db, project, target, extra_context="CHAT-CONTEXT-MARKER", include_memory=False
    )
    trigger = seen[-1]
    assert "CHAT-CONTEXT-MARKER" in trigger  # conversation context reaches the agent
    assert "STALE-MEMORY-MARKER" not in trigger  # unrelated shared memory does not

    planning.rerun_task(db, project, target)
    assert "STALE-MEMORY-MARKER" in seen[-1]  # default still reads shared memory


def test_handoff_names_the_next_work(db):
    """'Over to you' must name who picks up WHAT, not just a name."""
    org_id = _org(db)
    project = _run_project(db, org_id)
    done_msgs = [
        m.content for m in _status_messages(db, project) if "done with" in m.content
    ]
    assert done_msgs
    assert any("Over to you:" in m and "Draft technical spec" in m for m in done_msgs)


def test_kickoff_lists_opening_moves(db):
    org_id = _org(db)
    project = _run_project(db, org_id)
    first_msg = _status_messages(db, project)[0]
    assert "Opening moves" in first_msg.content


def test_chat_summary_references_the_request():
    art = type(
        "A",
        (),
        {"blocked": False, "needs_human": False, "content": "The deliverable body."},
    )()
    s = teamchat._summary(art, request="@mia write a positioning statement")
    assert "You asked me to" in s and "positioning statement" in s
    assert "The deliverable body." in s
    s2 = teamchat._summary(
        art
    )  # no request -> no opener (back-compat with plain artifact dumps)
    assert "You asked me to" not in s2 and "The deliverable body." in s2


def test_recent_transcript_shows_speakers_in_order(db):
    org_id = _org(db)
    mia = next(a for a in teamchat.roster(db, org_id) if teamchat.handle(a) == "mia")
    teamchat.post(db, org_id, "context line one about the launch")
    teamchat._reply(db, org_id, mia, "noted, standing by")
    out = teamchat.post(db, org_id, "@mia draft the announcement")
    db.commit()

    t = teamchat.recent_transcript(db, org_id)
    lines = t.splitlines()
    assert lines[0].startswith("the human:")  # human messages are attributed
    assert any(
        l.startswith("Mia Marketing Agent") for l in lines
    )  # agent replies attributed
    assert "@mia draft the announcement" in t  # the request itself is included
    assert out["tasks"]  # sanity: mention still created work
