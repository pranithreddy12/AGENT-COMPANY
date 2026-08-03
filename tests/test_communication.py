import pytest

from app.models import Organization
from app.services import communication as c


def _org(db):
    o = Organization(name="Acme")
    db.add(o)
    db.flush()
    return o.id


def test_thread_budget_auto_escalates(db):
    org_id = _org(db)
    thread = c.create_thread(db, org_id, "request", "help", message_budget=3)
    for i in range(3):
        c.post_message(db, thread, None, f"msg {i}")
    # the 4th message exceeds the budget -> escalate + refuse
    with pytest.raises(c.BudgetExceeded):
        c.post_message(db, thread, None, "one too many")
    assert thread.status == "escalated"


def test_handoff_records_packet_and_thread(db):
    org_id = _org(db)
    packet = c.make_handoff(db, org_id=org_id, project_id="p1", from_dept="d1", to_dept="d2",
                            context="Sales -> Dev", evidence=["scope note"])
    assert packet.from_department_id == "d1" and packet.evidence == ["scope note"]
