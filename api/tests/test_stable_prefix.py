"""Choosing the text a speculative generation runs on.

The rule changed on 2026-08-29, and the old one is why speculation was written
off. It fired on the common prefix of the last TWO partials, which can never
contain the newest word:

    partial "వన్"              fires on nothing
    partial "వన్ లాక్"          fires on "వన్"
    partial "వన్ లాక్ అండి"     fires on "వన్ లాక్"
    final   "వన్ లాక్ అండి"     MISS -- one word behind, always

These callers answer in two or three words, so a trigger that structurally
excludes the last word can never fire on a complete answer. The live probe duly
measured a 0% hit rate over nine turns, and the conclusion drawn at the time was
that speculation does not work on this traffic. The rule was the fault.

The concern the old rule protected against is real: Sarvam partials revise
BACKWARDS as the decoder re-scores (bench/FINDINGS.md 5b), so the newest partial
can contain words the caller never said. That is now handled where it belongs --
a contradicted speculation is cancelled, and the coordinator hands tokens over
only when the turn has genuinely ended AND the final text matches exactly. A
wrong guess costs tokens and can never reach the caller.
"""

from api.services.pipecat.speculation.stable_prefix import (
    Action,
    StablePrefixTracker,
)


def test_it_fires_on_everything_the_caller_has_said_so_far():
    """The newest partial IS the candidate -- last word included."""
    tracker = StablePrefixTracker()

    tracker.observe("అవును సార్")
    result = tracker.observe("అవును సార్ నా ఇల్లు")

    assert result.action is Action.FIRE
    assert result.stable_prefix == "అవును సార్ నా ఇల్లు"


def test_a_short_answer_is_speculated_on_in_full():
    """The case the old rule could never hit: a three-word Telugu answer."""
    tracker = StablePrefixTracker()
    tracker.observe("వన్")
    tracker.observe("వన్ లాక్")
    result = tracker.observe("వన్ లాక్ అండి")

    assert result.stable_prefix == "వన్ లాక్ అండి", (
        "the last word must be included or a short answer can never be a hit")


def test_a_partial_that_revises_backwards_makes_the_tracker_hold():
    """The exact sequence recorded from Sarvam in FINDINGS.md 5b.

    Still holds. A shrinking partial means the decoder is re-scoring, and
    starting another generation on unstable text would only waste it.
    """
    tracker = StablePrefixTracker()
    tracker.observe("అవును సార్ నా ఇల్లు నా")
    tracker.observe("అవును సార్ నా ఇల్లు నా ఇల్లు")

    result = tracker.observe("అవును సార్ నా ఇల్లు")  # shorter than before

    assert result.action is Action.HOLD


def test_it_does_not_refire_on_a_partial_that_has_not_grown():
    tracker = StablePrefixTracker()
    tracker.observe("అవును సార్ నా")

    assert tracker.observe("అవును సార్ నా").action is Action.HOLD


def test_an_empty_partial_fires_nothing():
    assert StablePrefixTracker().observe("").action is Action.HOLD
