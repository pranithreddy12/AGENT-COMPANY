import pytest

from app.services import cost


def test_cost_math():
    # sonnet: (3, 15) per 1M -> 1000 in, 2000 out
    got = cost.compute("claude-sonnet-5", 1000, 2000)
    assert got == round((1000 * 3 + 2000 * 15) / 1_000_000, 6)


def test_echo_is_free():
    assert cost.compute("echo-1", 999, 999) == 0.0


def test_unknown_model_fails_closed():
    with pytest.raises(cost.UnknownModelError):
        cost.compute("nope-9000", 10, 10)  # never a silent $0 fallback
