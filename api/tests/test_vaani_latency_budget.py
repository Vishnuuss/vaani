"""The budget file claims to be enforced. These tests make that claim true."""

import pytest

from api.services.vaani.latency import (
    BudgetExceeded,
    BudgetInvalid,
    Budget,
    check_breakdown,
    load_budget,
    report,
    validate_budget,
)


class _TTFB:
    def __init__(self, processor, duration_secs):
        self.processor = processor
        self.duration_secs = duration_secs


class _Breakdown:
    def __init__(self, user_turn_secs=None, ttfb=()):
        self.user_turn_secs = user_turn_secs
        self.ttfb = list(ttfb)


def test_the_shipped_budget_file_adds_up():
    """The yaml's own rule: components must not sum past the target."""
    validate_budget()


def test_a_budget_that_overspends_is_rejected():
    bad = Budget(target_p50_ms=800, target_p95_ms=1200,
                 components={"a": 500.0, "b": 400.0})
    with pytest.raises(BudgetInvalid):
        validate_budget(bad)


def test_a_turn_inside_budget_reports_nothing():
    budget = load_budget()
    allowed = budget.allocation("endpoint_detection", "stt_finalize")
    b = _Breakdown(user_turn_secs=(allowed - 50) / 1000)
    assert check_breakdown(b) == []


def test_the_measured_run_110_endpoint_is_flagged():
    """Run 110 spent ~1.2s before the LLM. That must not pass silently."""
    violations = check_breakdown(_Breakdown(user_turn_secs=1.2))
    assert len(violations) == 1
    assert "user_turn_secs" in violations[0].scope
    assert violations[0].actual_ms == pytest.approx(1200)


def test_a_slow_llm_is_attributed_to_the_llm():
    """Run 110 turn 2: 3.084s LLM TTFB against a 200ms allocation."""
    violations = check_breakdown(_Breakdown(ttfb=[_TTFB("GroqLLMService#0", 3.084)]))
    assert len(violations) == 1
    assert "llm_first_spoken_token" in violations[0].scope


def test_a_fast_tts_is_not_flagged():
    """Cartesia measured 0.091s against a 190ms allocation -- genuinely fine."""
    assert check_breakdown(_Breakdown(ttfb=[_TTFB("CartesiaTTSService#0", 0.091)])) == []


def test_unknown_processors_are_ignored_not_guessed():
    assert check_breakdown(_Breakdown(ttfb=[_TTFB("SomeOtherProcessor", 9.0)])) == []


def test_prod_warns_and_never_raises(monkeypatch):
    monkeypatch.delenv("VAANI_LATENCY_ENFORCEMENT", raising=False)
    assert report(_Breakdown(user_turn_secs=1.2), turn=3)  # returns, does not raise


def test_strict_raises(monkeypatch):
    monkeypatch.setenv("VAANI_LATENCY_ENFORCEMENT", "strict")
    with pytest.raises(BudgetExceeded):
        report(_Breakdown(user_turn_secs=1.2), turn=3)


def test_a_broken_breakdown_never_breaks_the_call(monkeypatch):
    class Exploding:
        @property
        def user_turn_secs(self):
            raise RuntimeError("boom")

    monkeypatch.setenv("VAANI_LATENCY_ENFORCEMENT", "strict")
    assert report(Exploding(), turn=1) == []
