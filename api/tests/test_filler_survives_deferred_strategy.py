"""The filler player must find the analyzer even through a deferred wrapper.

Defect 3 of the four that got semantic turn completion reverted after run 792,
and the nastiest, because nothing failed and nothing logged.

`analyzer_from` looks for `_turn_analyzer` / `turn_analyzer` on each strategy.
`DeferredUserTurnStopStrategy` -- the wrapper semantic turn completion puts
around the real strategy -- holds it in `.inner` and defines neither of those
names, nor `__getattr__`. So the lookup fell through and returned None.

A None analyzer does not raise. `FillerPlayer` is simply constructed inert and
never speaks. So enabling semantic turn completion SWITCHED OFF the one feature
covering dead air, at exactly the moment deferral made the dead air longer.

This is a behavioural test on the real classes. The guard that let this ship the
first time asserted `"_strategy" in src or "getattr" in src` against source text
and passed whether or not the analyzer was reachable.
"""

from __future__ import annotations

from pipecat.turns.user_stop.deferred_user_turn_stop_strategy import (
    DeferredUserTurnStopStrategy,
)

from api.services.vaani.turn_taking import analyzer_from


class _Analyzer:
    pass


class _Strategy:
    def __init__(self, analyzer):
        self._turn_analyzer = analyzer


def test_a_bare_strategy_still_works():
    a = _Analyzer()
    assert analyzer_from([_Strategy(a)]) is a


def test_the_analyzer_survives_a_deferred_wrapper():
    """Run 792: this returned None and the fillers went silent."""
    a = _Analyzer()
    wrapped = DeferredUserTurnStopStrategy(_Strategy(a))
    assert analyzer_from([wrapped]) is a


def test_it_survives_a_wrapper_around_a_wrapper():
    a = _Analyzer()
    inner = DeferredUserTurnStopStrategy(_Strategy(a))
    assert analyzer_from([DeferredUserTurnStopStrategy(inner)]) is a


def test_a_strategy_with_no_analyzer_is_still_None():
    """The negative case, so the test cannot pass by always finding something."""
    class _Empty:
        pass
    assert analyzer_from([_Empty()]) is None
    assert analyzer_from([DeferredUserTurnStopStrategy(_Empty())]) is None


def test_it_does_not_loop_forever_on_a_self_referencing_wrapper():
    class _Loop:
        pass
    loop = _Loop()
    loop.inner = loop
    assert analyzer_from([loop]) is None
