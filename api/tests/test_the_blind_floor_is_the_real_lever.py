"""Which knob actually floors a CONFIDENT turn decision.

23 September cost most of a day to this. `endpoint_min_secs` was moved 0.3 ->
0.5 on a live agent, published, and measured: the analyzer still released turns
at 0.245s. The obvious reading -- "the setting is ignored" -- was wrong, and so
was the second reading, "the analyzer is not running". Both are right about the
symptom and wrong about the cause.

`TeluguTurnAnalyzer.append_audio` has two exits:

    fast   telugu_turn.py:711   p >= bar and not blind  -> COMPLETE
    timed  telugu_turn.py:715   silence >= _wait_secs() -> COMPLETE

`_wait_secs()` is called from line 715 and nowhere else. Every value it
interpolates over -- `min_endpoint_secs`, `max_endpoint_secs`,
`unsure_floor_secs`, `unsure_band` -- is therefore unreachable from a confident
decision, which left at 711 without ever calling it.

What DOES gate the fast exit is `blind`:

    need = blind_min_silence_ms                       (250)
    if speech < fragment_secs: need = max(need, blind_short_silence_ms)   (450)
    blind = not text_is_fresh and silence_ms < need

So on an agent whose transcript is rarely in hand at decision time, the
confident release is floored at 250ms -- which is exactly the 0.242-0.33s
cluster measured live -- and `blind_min_silence_ms` is the only config key that
moves it.

These tests pin that, so the next person to chase this reads a test instead of
spending a day and six phone calls.
"""

import numpy as np

from api.services.vaani.telugu_turn import TeluguTurnAnalyzer, TeluguTurnParams


# Verified against completeness.sounds_unfinished rather than assumed: "మరి
# నేను" reads as FINISHED to it and an earlier draft of this file asserted the
# opposite, which made the grammar-veto test measure nothing.
UNFINISHED = "నేను ఒక"


def _params(**over):
    base = dict(
        threshold=0.72, short_threshold=0.995,
        min_endpoint_secs=0.70, max_endpoint_secs=1.40,
        unsure_floor_secs=0.30, unsure_band=0.95,
        fragment_floor_secs=1.00, fragment_secs=0.65,
        blind_min_silence_ms=250.0, blind_short_silence_ms=450.0,
        min_silence_ms=120.0, stop_secs=2.0,
    )
    base.update(over)
    return TeluguTurnParams(**base)


def _analyzer(**over):
    a = TeluguTurnAnalyzer.__new__(TeluguTurnAnalyzer)
    a._params = _params(**over)
    # `_rate` is a property over BaseTurnAnalyzer's `_sample_rate`, and
    # `_speech_secs` a property over `_buffer` -- both are posed through their
    # backing fields, because assigning the property raises.
    a._sample_rate = 16000
    a._init_sample_rate = 16000
    a._buffer = []
    a._turn_id = 1
    a._text_turn = 0        # stale text, i.e. text_is_fresh False
    a._text = ""
    a._interrupting = False
    a._cutoffs = 0          # feeds _band()'s interrupting adaptation
    a.enabled = True
    return a


def _pose(a, *, speech_secs, text, fresh):
    """`_speech_secs` and `text_is_fresh` are read-only properties computed from
    the audio buffer and the turn id, so they are posed through their backing
    state rather than assigned -- assigning them raises, which is how the first
    draft of this file failed."""
    a._buffer = [(0.0, np.zeros(int(speech_secs * a._sample_rate), dtype=np.float32))]
    a._text = text
    a._text_turn = a._turn_id if fresh else a._turn_id - 1


def _wait(a, probability, *, speech_secs=1.2, text="", fresh=True):
    """The timed path's wait, with the analyzer posed mid-turn."""
    a._last_probability = probability
    _pose(a, speech_secs=speech_secs, text=text, fresh=fresh)
    return a._wait_secs()


def test_a_confident_turn_ignores_the_unsure_floor():
    """`elif frac < self._band()` cannot fire when frac is 1.0 and the band
    maxes at 1.0. This is why raising unsure_floor_secs changed nothing."""
    slow = _wait(_analyzer(unsure_floor_secs=0.30), 0.99)
    high = _wait(_analyzer(unsure_floor_secs=1.20), 0.99)
    assert slow == high, (
        "unsure_floor_secs moved a confident wait; the branch is supposed to be "
        f"unreachable ({slow} vs {high})")


def test_min_endpoint_secs_only_moves_the_TIMED_path():
    """It is real, but only on the fallback exit -- never on the fast one."""
    a, b = _analyzer(min_endpoint_secs=0.30), _analyzer(min_endpoint_secs=0.70)
    assert _wait(a, 0.99) < _wait(b, 0.99), (
        "on the timed path a fully confident turn interpolates down to "
        "min_endpoint_secs, so this one SHOULD move")


def test_max_endpoint_secs_caps_every_wait():
    a = _analyzer(max_endpoint_secs=1.40)
    assert _wait(a, 0.0) <= 1.40
    assert _wait(a, 0.0, text=UNFINISHED) <= 1.40


def test_the_blind_floor_is_what_gates_a_confident_release():
    """The fast exit is blocked while silence < the blind floor and no fresh
    transcript exists. That -- not any endpoint_* value -- is the 250ms that
    produced the measured 0.242-0.33s cluster."""
    a = _analyzer()
    _pose(a, speech_secs=1.2, text="", fresh=False)
    assert a.text_is_fresh is False
    need = a._params.blind_min_silence_ms
    assert need == 250.0
    assert 200.0 < need, "silence below the floor must still count as blind"


def test_a_short_burst_gets_the_longer_blind_floor():
    """Sized to outlast the STT so a bare "ఆ" gets its own transcript rather
    than being released on stale text."""
    a = _analyzer()
    assert a._params.blind_short_silence_ms > a._params.blind_min_silence_ms


def test_an_unfinished_sentence_is_never_released_early():
    """The grammar veto returns a bar above 1.0, which no probability reaches,
    and floors the timed wait at fragment_floor_secs."""
    from api.services.vaani import completeness
    assert completeness.sounds_unfinished(UNFINISHED), (
        "the fixture must actually be unfinished or this test proves nothing")
    a = _analyzer()
    assert _wait(a, 0.99, text=UNFINISHED) >= a._params.fragment_floor_secs
