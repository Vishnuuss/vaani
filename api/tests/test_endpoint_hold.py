"""The caller must be allowed to finish his sentence.

Run 295, 2026-08-29, in the caller's own words as the call fell apart:

    07:53:51.667  "ఇంకా చెప్పలే కదా బిల్లు"        I haven't told you the bill yet
    07:53:53.587  "అప్పుడే మీరు క్వశ్చన్"          you're already on the next question
    07:54:00.311  "అసలు మిమ్మల్ని ఆన్సర్ చెప్పనియ్యరా మీరు?"  do you even let me answer?
    07:54:01                                       (hangs up)

Cause, from the stored workflow configuration: VAD stop 0.2 s plus analyzer
stop 0.2 s. Nobody on that call was ever allowed to pause for more than about
0.4 s. Saying "60 ... aaa ... 70" out loud takes longer than that, so the turn
was cut at "60" and `monthly_bill: 62` was stored.

These tests pin the property that fixes it: the wait is a FUNCTION of how
finished the caller sounds, not a constant.
"""

import numpy as np
import pytest

from api.services.vaani.completeness import sounds_unfinished
from api.services.vaani.telugu_turn import (
    MIN_CONFIDENT_TURN_S,
    TeluguTurnAnalyzer,
    TeluguTurnParams,
)


def analyzer(**kw) -> TeluguTurnAnalyzer:
    a = TeluguTurnAnalyzer(params=TeluguTurnParams(
        stop_secs=0.2, min_endpoint_secs=0.05, max_endpoint_secs=1.40,
        fragment_floor_secs=0.45, **kw))
    a.set_sample_rate(8000)
    return a


def fill(a: TeluguTurnAnalyzer, secs: float) -> None:
    """Give the analyzer `secs` of audio so `_speech_secs` is realistic."""
    a._buffer = [(0.0, np.zeros(int(secs * 8000), dtype=np.int16))]


# --- the core property -----------------------------------------------------

def test_a_caller_who_sounds_unfinished_is_given_longer():
    """The whole fix in one assertion."""
    a = analyzer()
    fill(a, 2.0)

    a._last_probability = 0.95          # nearly finished
    confident = a._wait_secs()

    a._last_probability = 0.02          # clearly still going
    unsure = a._wait_secs()

    assert unsure > confident, (
        "a caller mid-sentence must get more silence than one who has stopped")
    # Near the ceiling, not exactly at it: the wait is interpolated, so a
    # p of 0.02 against a 0.97 bar lands a shade inside 1.40.
    assert unsure >= 1.35


def test_the_confident_case_is_not_made_slower():
    """The 700-800ms budget is paid for out of this branch.

    Whatever else changes, a caller the model is sure about must still be
    answered as fast as before the two-sided window existed.
    """
    a = analyzer()
    fill(a, 2.0)
    a._last_probability = 0.969         # just under the 0.97 bar
    assert a._wait_secs() <= 0.2, "no slower than the old fixed timeout"


def test_a_bare_number_holds_the_turn_open():
    """The "60 ... aaa ... 70" case, which stored monthly_bill 62."""
    a = analyzer()
    fill(a, 2.0)
    a._last_probability = 0.9           # sounds finished -- it is one word
    a.note_text("అరవై")                  # ...but it is a quantity, not an answer
    assert a._wait_secs() >= 0.45


def test_the_same_number_with_a_unit_does_not():
    a = analyzer()
    fill(a, 2.0)
    a._last_probability = 0.9
    a.note_text("అరవై వేలు")             # sixty thousand -- a complete answer
    assert a._wait_secs() < 0.45


def test_a_hesitation_is_never_treated_as_an_answer():
    """Run 295 answered "ఆ మరి ఆ." as though it were the monthly bill."""
    a = analyzer()
    fill(a, 2.0)
    a._last_probability = 0.99
    a.note_text("ఆ మరి ఆ.")
    assert a._wait_secs() >= 0.45


def test_a_short_utterance_gets_the_floor_whatever_the_text():
    """Run 287: "మాది." was a breath, not an answer, and was answered."""
    a = analyzer()
    fill(a, MIN_CONFIDENT_TURN_S - 0.1)
    a._last_probability = 0.96
    assert a._wait_secs() >= 0.45


def test_a_long_confident_utterance_is_still_fast():
    a = analyzer()
    fill(a, 3.0)
    a._last_probability = 0.96
    a.note_text("నా ఇల్లు హైదరాబాద్‌లో ఉంది సార్")
    assert a._wait_secs() <= 0.2


# --- the safety properties -------------------------------------------------

def test_resumed_speech_resets_the_silence_timer():
    """The mechanism the hold exists to serve.

    Holding is only useful if the caller speaking again cancels the countdown --
    otherwise the turn ends late instead of ending wrong.
    """
    a = analyzer()
    silence = np.zeros(800, dtype=np.int16).tobytes()

    a.append_audio(np.ones(800, dtype=np.int16).tobytes(), True)
    for _ in range(3):
        a.append_audio(silence, False)
    assert a._silence_ms > 0

    a.append_audio(np.ones(800, dtype=np.int16).tobytes(), True)
    assert a._silence_ms == 0, "speech must cancel the pending end of turn"


def test_the_wait_is_never_longer_than_the_declared_ceiling():
    """A caller who has genuinely stopped must not sit in silence."""
    a = analyzer()
    fill(a, 0.2)                        # short: fragment floor applies
    a._last_probability = 0.0           # and the model says still going
    a.note_text("ఆ")                     # and the text says still going
    assert a._wait_secs() <= 1.40


def test_no_model_means_exactly_the_old_behaviour():
    """If the weights are missing nothing about today changes."""
    a = analyzer()
    a.enabled = False
    a._last_probability = None
    assert a._wait_secs() == 0.2


def test_a_turn_boundary_clears_the_text():
    """Stale text from the previous turn must not hold the next one open."""
    a = analyzer()
    a.note_text("అరవై")
    a.clear()
    assert not sounds_unfinished(a._text)


def test_a_long_calm_utterance_ending_on_a_number_cannot_end_early():
    """The hole the fragment floor alone does not cover.

    "నా బిల్లు దాదాపు అరవై" is long, falling and unhurried -- every acoustic
    feature the model was trained on says the caller has finished. He has not:
    the unit is still to come. No reachable threshold fixes this, because the
    prosody really does say COMPLETE. So the early path is closed outright and
    the turn ends on the timed path, which the floor bounds at 0.45 s.
    """
    a = analyzer()
    fill(a, 3.0)
    a.note_text("నా బిల్లు దాదాపు అరవై")

    assert a._bar() > 1.0, "no probability may end this turn early"
    assert a._wait_secs() >= 0.45


def test_the_early_path_is_open_again_once_the_unit_arrives():
    a = analyzer()
    fill(a, 3.0)
    a.note_text("నా బిల్లు దాదాపు అరవై వేలు")
    assert a._bar() <= 1.0
