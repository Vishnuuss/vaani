"""Speculative-turn accounting.

The <700 ms budget rests entirely on the speculation HIT RATE, which has never
been measured. This component is what measures it, so its accounting has to be
honest: a response generated from text the caller did not finish saying is NOT
the same as a clean hit, and the two must never be collapsed.
"""

import pytest

from api.services.pipecat.speculation.speculator import (
    Outcome,
    SpecAction,
    Speculator,
)


def test_it_fires_once_the_prefix_is_stable():
    spec = Speculator()
    spec.on_partial("నా ఇల్లు")
    command = spec.on_partial("నా ఇల్లు నాదే")

    assert command.action is SpecAction.FIRE
    assert command.text == "నా ఇల్లు"


def test_it_cancels_when_the_caller_turns_out_to_have_said_something_else():
    spec = Speculator()
    spec.on_partial("నాకు లోన్")
    spec.on_partial("నాకు లోన్ కావాలి")  # fires on "నాకు లోన్"

    # The decoder re-scores and the second word was wrong all along.
    command = spec.on_partial("నాకు ఇల్లు కావాలి")

    assert command.action is SpecAction.CANCEL


def test_an_exact_match_at_turn_end_is_a_hit():
    spec = Speculator()
    spec.on_partial("నా ఇల్లు")
    spec.on_partial("నా ఇల్లు నాదే")  # fires on "నా ఇల్లు"

    outcome = spec.on_turn_end("నా ఇల్లు")

    assert outcome is Outcome.HIT
    assert spec.stats.hits == 1


def test_speaking_before_the_caller_finished_is_not_counted_as_a_hit():
    spec = Speculator()
    spec.on_partial("నా ఇల్లు")
    spec.on_partial("నా ఇల్లు నాదే")  # fires on "నా ఇల్లు"

    # The caller kept going. Our speculation was on a genuine prefix, but the
    # response was built without the rest of the sentence.
    outcome = spec.on_turn_end("నా ఇల్లు నాదే కాదు అద్దె")

    assert outcome is Outcome.PARTIAL
    assert spec.stats.hits == 0


def test_speculating_on_the_wrong_words_is_a_miss():
    spec = Speculator()
    spec.on_partial("నాకు లోన్")
    spec.on_partial("నాకు లోన్ కావాలి")  # fires on "నాకు లోన్"

    outcome = spec.on_turn_end("నాకు ఇల్లు కావాలి")

    assert outcome is Outcome.MISS
    assert spec.stats.misses == 1


def test_a_turn_we_never_speculated_on_is_recorded_but_is_not_a_miss():
    spec = Speculator()

    outcome = spec.on_turn_end("సరే")

    assert outcome is Outcome.NO_SPECULATION
    assert spec.stats.misses == 0
    assert spec.stats.turns == 1


def test_hit_rate_counts_only_clean_hits_over_all_turns():
    spec = Speculator()

    for text in ("అవును", "అవును సరే"):
        spec.on_partial(text)
    spec.on_turn_end("అవును")  # HIT

    spec.reset_turn()
    for text in ("కాదు", "కాదు లేదు"):
        spec.on_partial(text)
    spec.on_turn_end("వేరే మాట")  # MISS

    assert spec.stats.turns == 2
    assert spec.stats.hits == 1
    assert spec.stats.hit_rate == pytest.approx(0.5)
