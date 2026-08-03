import pytest

from app.services import scheduling

# Diamond: t1 -> t2 -> {t3(5h), t4(2h)} -> t5.  t4 has slack.
DIAMOND = [
    {"id": "t1", "effort": 2.0, "deps": []},
    {"id": "t2", "effort": 3.0, "deps": ["t1"]},
    {"id": "t3", "effort": 5.0, "deps": ["t2"]},
    {"id": "t4", "effort": 2.0, "deps": ["t2"]},
    {"id": "t5", "effort": 2.0, "deps": ["t3", "t4"]},
]


def test_critical_path_and_slack():
    slots, finish, crit = scheduling.schedule([dict(n) for n in DIAMOND])
    assert finish == 12.0  # 2+3+5+2
    assert crit == ["t1", "t2", "t3", "t5"]  # t4 is off the path
    assert slots["t4"].slack == 3.0  # 5h branch vs 2h branch
    assert slots["t1"].slack == 0.0


def test_slip_shifts_critical_path():
    nodes = [dict(n) for n in DIAMOND]
    # slip t4 by +4h (2 -> 6): its branch (3+6) now beats t3's (3+5)
    slots, finish, crit = scheduling.apply_slip(nodes, "t4", 4.0)
    assert finish == 13.0  # 2+3+6+2
    assert "t4" in crit and "t3" not in crit
    assert slots["t3"].slack == 1.0


def test_cycle_raises():
    cyclic = [{"id": "a", "effort": 1, "deps": ["b"]}, {"id": "b", "effort": 1, "deps": ["a"]}]
    with pytest.raises(scheduling.CycleError):
        scheduling.schedule(cyclic)


def test_slip_risk():
    assert scheduling.slip_risk(remaining_hours=2.0, est_effort=2.0) is True  # 2 < 2*1.2
    assert scheduling.slip_risk(remaining_hours=3.0, est_effort=2.0) is False
