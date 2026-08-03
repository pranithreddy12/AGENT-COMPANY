"""Deterministic cost accounting. Code, never a model call. Fails closed on unknown models.

Rates are USD per 1M tokens (input, output). Illustrative — calibrate against the
provider's price sheet.
# ponytail: hardcoded rate table; move to DB per-org when pricing needs to vary per tenant.
"""

RATES: dict[str, tuple[float, float]] = {
    "echo-1": (0.0, 0.0),
    "echo-priced": (10.0, 30.0),  # used only to exercise cost-cap logic in tests
    "claude-opus-4-8": (15.0, 75.0),
    "claude-opus-5": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


class UnknownModelError(Exception):
    pass


def register_free(model: str) -> None:
    """Register a model as zero-cost (local models: Ollama, etc.). Keeps compute() fail-closed
    for genuinely unknown paid models while letting local runs through."""
    RATES.setdefault(model, (0.0, 0.0))


def compute(model: str, input_tokens: int, output_tokens: int) -> float:
    if model not in RATES:
        raise UnknownModelError(model)  # fail closed: no silent $0 fallback
    in_rate, out_rate = RATES[model]
    return round((input_tokens * in_rate + output_tokens * out_rate) / 1_000_000, 6)
