"""Stable-prefix tracking over non-monotonic STT partials.

Measured behaviour this exists to handle (vaani/bench/FINDINGS.md §5b): Sarvam
partials revise *backwards* as the decoder re-scores. Firing a speculative LLM
call on the raw latest partial would fire on words the caller never said.
"""

from api.services.pipecat.speculation.stable_prefix import (
    Action,
    StablePrefixTracker,
)


def test_a_word_is_stable_once_two_consecutive_partials_agree_on_it():
    tracker = StablePrefixTracker()

    tracker.observe("అవును సార్")
    result = tracker.observe("అవును సార్ నా ఇల్లు")

    # "అవును సార్" appeared in both partials, so it is confirmed. "నా ఇల్లు"
    # has only been seen once and may still be revised away.
    assert result.stable_prefix == "అవును సార్"


def test_a_partial_that_revises_backwards_makes_the_tracker_hold():
    # The exact sequence recorded from Sarvam in FINDINGS.md 5b.
    tracker = StablePrefixTracker()
    tracker.observe("అవును సార్ నా ఇల్లు నా")
    tracker.observe("అవును సార్ నా ఇల్లు నా ఇల్లు")

    result = tracker.observe("అవును సార్ నా ఇల్లు")  # shorter than before

    assert result.action is Action.HOLD


def test_it_fires_when_the_stable_prefix_grows():
    tracker = StablePrefixTracker()
    tracker.observe("అవును సార్ నా")

    # Agrees with the previous partial on three words, so they become stable.
    result = tracker.observe("అవును సార్ నా ఇల్లు")

    assert result.action is Action.FIRE
    assert result.stable_prefix == "అవును సార్ నా"
