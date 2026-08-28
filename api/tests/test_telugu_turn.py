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
    a = TeluguTurnAnalyzer(sample_rate=SR, weights_path=tmp_path / "nope.json")
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
    a = TeluguTurnAnalyzer(sample_rate=SR)
    assert a.enabled, "the exported weights must ship with the package"
    assert a._coef is not None and len(a._coef) == 16
    assert 0.5 <= a.params.threshold <= 1.0


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
