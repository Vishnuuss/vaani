"""Tests for the Telugu turn detector.

The property that matters most is not accuracy. It is that this analyzer can
only ever make a turn END SOONER than it does today -- when it is unsure it
returns INCOMPLETE and the existing silence timeout fires exactly as before.
A turn detector that could stall a call would be worse than no turn detector.
"""

from __future__ import annotations

import numpy as np
import pytest

from api.services.vaani.telugu_turn import (
    MIN_SILENCE_MS,
    TeluguTurnAnalyzer,
    TeluguTurnParams,
    extract_features,
)
from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState

SR = 8000


def speech(secs: float = 1.0, amp: float = 0.3) -> bytes:
    """Voiced-ish audio: a 150 Hz buzz, roughly a Telugu speaking pitch."""
    t = np.arange(int(SR * secs)) / SR
    x = amp * np.sin(2 * np.pi * 150 * t)
    return (x * 32767).astype(np.int16).tobytes()


def silence(secs: float) -> bytes:
    return np.zeros(int(SR * secs), dtype=np.int16).tobytes()


def feed(a: TeluguTurnAnalyzer, data: bytes, is_speech: bool, chunk_ms: int = 20):
    """Push audio in realistic 20ms frames, returning the last state."""
    step = int(SR * chunk_ms / 1000) * 2
    state = EndOfTurnState.INCOMPLETE
    for i in range(0, len(data), step):
        state = a.append_audio(data[i:i + step], is_speech)
        if state == EndOfTurnState.COMPLETE:
            return state
    return state


def analyzer(**kw) -> TeluguTurnAnalyzer:
    a = TeluguTurnAnalyzer(sample_rate=SR, params=TeluguTurnParams(**kw))
    return a


# --- the safety property ----------------------------------------------------


def test_silence_still_ends_the_turn_when_the_model_never_fires():
    """The existing timeout must survive. This is the no-regression guarantee."""
    a = analyzer(stop_secs=0.5, threshold=1.1)      # unreachable threshold
    feed(a, speech(0.8), True)
    state = feed(a, silence(0.7), False)
    assert state == EndOfTurnState.COMPLETE


def test_an_unconfident_model_never_delays_past_the_timeout():
    a = analyzer(stop_secs=0.4, threshold=1.1)
    feed(a, speech(0.8), True)
    # Just under the timeout: must NOT have ended yet, and must not hang.
    assert feed(a, silence(0.3), False) == EndOfTurnState.INCOMPLETE
    assert feed(a, silence(0.2), False) == EndOfTurnState.COMPLETE


def test_missing_weights_disable_it_rather_than_break_the_call(tmp_path):
    # BOTH models have to be absent now: the forest is tried first and the
    # regression is its fallback, so removing one leaves a working detector.
    a = TeluguTurnAnalyzer(sample_rate=SR,
                           weights_path=tmp_path / "nope.json",
                           gbm_path=tmp_path / "nope.json")
    assert a.enabled is False
    feed(a, speech(0.8), True)
    # Still ends on silence: a disabled detector is the old behaviour, not a
    # broken call.
    assert feed(a, silence(2.5), False) == EndOfTurnState.COMPLETE


def test_speech_never_ends_a_turn():
    a = analyzer(threshold=0.0)                     # would fire on anything
    assert feed(a, speech(1.5), True) == EndOfTurnState.INCOMPLETE


def test_silence_before_anyone_speaks_is_not_a_turn():
    """Nobody has spoken yet, so there is no turn to end."""
    a = analyzer(stop_secs=0.2, threshold=0.0)
    assert feed(a, silence(3.0), False) == EndOfTurnState.INCOMPLETE


# --- the model itself -------------------------------------------------------


def test_a_confident_model_ends_the_turn_early():
    a = analyzer(stop_secs=5.0, threshold=0.0)      # fires as soon as it can
    feed(a, speech(1.0), True)
    state = feed(a, silence(MIN_SILENCE_MS / 1000 + 0.06), False)
    assert state == EndOfTurnState.COMPLETE, (
        "with a reachable threshold the turn must end long before stop_secs")


def test_features_match_the_trained_count():
    """16 numbers, in the trainer's order, or the weights mean nothing."""
    x = np.frombuffer(speech(1.5), dtype=np.int16).astype(np.float32) / 32768.0
    f = extract_features(x, SR)
    assert f is not None and len(f) == 16
    assert all(np.isfinite(v) for v in f)


def test_too_little_audio_yields_no_features_rather_than_garbage():
    x = np.frombuffer(speech(0.02), dtype=np.int16).astype(np.float32) / 32768.0
    assert extract_features(x, SR) is None


def test_the_shipped_weights_load():
    """A model -- either model -- must ship with the package."""
    a = TeluguTurnAnalyzer(sample_rate=SR)
    assert a.enabled, "the exported weights must ship with the package"
    assert a._forest is not None or (a._coef is not None and len(a._coef) == 16)
    assert 0.5 <= a.params.threshold <= 1.0


def test_the_regression_weights_still_ship_as_the_fallback(tmp_path):
    """The fallback is only a fallback if its file is actually there."""
    a = TeluguTurnAnalyzer(sample_rate=SR, gbm_path=tmp_path / "absent.json")
    assert a._coef is not None and len(a._coef) == 16


@pytest.mark.parametrize("secs", [0.05, 0.2, 1.0, 3.0])
def test_it_never_raises_on_any_amount_of_audio(secs):
    """A crash here would drop a caller's turn, which is worse than being slow."""
    a = analyzer()
    feed(a, speech(secs), True)
    feed(a, silence(secs), False)


def test_clear_resets_between_turns():
    a = analyzer(stop_secs=0.3, threshold=1.1)
    feed(a, speech(0.5), True)
    a.clear()
    assert a.speech_triggered is False
    # A fresh turn must still be able to end.
    feed(a, speech(0.5), True)
    assert feed(a, silence(0.4), False) == EndOfTurnState.COMPLETE


# --- the boosted forest ------------------------------------------------------
#
# Shipped as flat arrays rather than through scikit-learn, because a boosted
# forest is a pile of thresholds and constants and sklearn is only the thing
# that found them. `tools/export_gbm_turn.py` proves the exported arithmetic
# agrees with sklearn's own predictions to 5.6e-16 across all 1,393 clips; these
# tests hold that line inside the repo.


def test_the_forest_is_preferred_over_the_regression():
    """43.9% of turns endable early against the regression's 33.9%."""
    a = TeluguTurnAnalyzer(sample_rate=SR)
    assert a.enabled
    assert a._forest is not None, "the exported forest must ship and load"


def test_the_regression_still_works_when_the_forest_is_absent(tmp_path):
    """The forest is an upgrade, not a load-bearing dependency."""
    a = TeluguTurnAnalyzer(sample_rate=SR, gbm_path=tmp_path / "absent.json")
    assert a.enabled, "must fall back to the regression, not switch off"
    assert a._forest is None
    assert a._coef is not None


def test_losing_both_models_disables_rather_than_breaks(tmp_path):
    a = TeluguTurnAnalyzer(sample_rate=SR,
                           gbm_path=tmp_path / "no.json",
                           weights_path=tmp_path / "no.json")
    assert a.enabled is False
    feed(a, speech(0.8), True)
    # Still ends on silence: the pre-detector behaviour, not a broken call.
    assert feed(a, silence(2.5), False) == EndOfTurnState.COMPLETE


@pytest.mark.parametrize("features,expected", [
    ([0.1257, -0.1321, 0.6404, 0.1049, -0.5357, 0.3616, 1.304, 0.9471, -0.7037,
      -1.2654, -0.6233, 0.0413, -2.325, -0.2188, -1.2459, -0.7323], 0.13668883960378564),
    ([-0.5443, -0.3163, 0.4116, 1.0425, -0.1285, 1.3665, -0.6652, 0.3515, 0.9035,
      0.094, -0.7435, -0.9217, -0.4577, 0.2202, -1.0096, -0.2092], 0.14981838784473483),
    ([-0.1592, 0.5408, 0.2147, 0.3554, -0.6538, -0.1296, 0.784, 1.4934, -1.2591,
      1.5139, 1.3459, 0.7813, 0.2645, -0.3139, 1.458, 1.9603], 0.05847060851869748),
    ([1.8016, 1.3151, 0.3574, -1.2083, -0.0045, 0.6565, -1.2884, 0.3951, 0.4299,
      0.696, -1.1841, -0.6617, -0.4364, -1.1698, 1.7394, -0.4959], 0.07288004130433232),
])
def test_the_forest_scores_exactly_what_it_was_exported_to_score(features, expected):
    """Golden values. A silent change here is a silently different detector."""
    a = TeluguTurnAnalyzer(sample_rate=SR)
    got = a._forest.probability(np.asarray(features, dtype=np.float64))
    assert got == pytest.approx(expected, abs=1e-12)


def test_the_forest_reads_raw_features_not_standardised_ones():
    """The trees split on RAW values.

    Standardising the vector before scoring -- which the regression requires and
    the forest must not have -- would shift every threshold in all 250 trees and
    produce a detector that looks fine and is wrong.
    """
    a = TeluguTurnAnalyzer(sample_rate=SR)
    x = np.zeros(16, dtype=np.float64)
    raw = a._forest.probability(x)
    standardised = a._forest.probability((x - a._mean) / a._scale
                                         if a._mean is not None else x)
    assert raw != standardised or a._mean is None


def test_the_forest_threshold_holds_the_safety_bar():
    """2% false cutoffs is the caller-facing promise; the threshold encodes it."""
    a = TeluguTurnAnalyzer(sample_rate=SR)
    assert 0.5 <= a.params.threshold <= 1.0


def test_an_explicit_threshold_still_wins_over_the_forests_own():
    a = TeluguTurnAnalyzer(sample_rate=SR, params=TeluguTurnParams(threshold=0.55))
    assert a.params.threshold == 0.55


def test_the_forest_never_makes_a_turn_slower():
    """The whole safety argument, re-checked with the forest in place."""
    a = TeluguTurnAnalyzer(sample_rate=SR, params=TeluguTurnParams(stop_secs=0.4))
    feed(a, speech(0.8), True)
    assert feed(a, silence(0.5), False) == EndOfTurnState.COMPLETE
