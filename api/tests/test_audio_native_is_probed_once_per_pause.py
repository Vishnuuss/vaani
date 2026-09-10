"""The encoder must run when the AUDIO changes, not when the clock moves.

The bug this locks out
----------------------
`TeluguTurnAnalyzer.append_audio` calls `_probability()` on EVERY audio frame
once silence passes `min_silence_ms`, and frames arrive every 20 ms. Between
that 120 ms floor and the 1.4 s endpoint ceiling it is about **64 calls per
turn**.

The forest costs ~1 ms and never noticed. The encoder costs 50 ms on an idle
box and over 200 ms on a loaded one, so 64 calls is 3-13 seconds of CPU inside
a frame handler that has 20 ms to return. That is not a slow agent, it is a
broken one.

Why a time-based throttle is the wrong fix, and was measured to be
------------------------------------------------------------------
A wall-clock interval was tried first. It still let **21** inferences through,
because each inference takes longer than the frame it is standing in, so the
loop falls behind and the interval keeps elapsing. Throttling on time treats
the symptom.

The right key is what the model actually reads. Its input is the last 8 s of
the caller's audio; while he is silent the only thing changing is the amount of
trailing silence, so the speech in the window -- and therefore the verdict --
is identical. What silence DURATION means is already the timers' job, and they
still run on every frame.

One more trap, also measured: `_speech_secs` is the whole buffer INCLUDING
silence, so it grows every frame and is useless as a cache key. It let 32
inferences through per turn. The trailing silence has to be subtracted.
"""

import numpy as np
import pytest

import api.services.vaani.audio_native_turn as ant
from api.services.vaani.telugu_turn import TeluguTurnParams

FRAME_MS = 20
RATE = 8000


def _analyzer():
    a = ant.AudioNativeTurnAnalyzer(
        sample_rate=RATE,
        params=TeluguTurnParams(min_endpoint_secs=0.30,
                                max_endpoint_secs=1.40,
                                unsure_band=0.90))
    if not a.enabled or a._model is None:
        pytest.skip("no exported model on this checkout")
    return a


class _Counter:
    """Count real inferences by patching the CLASS, not the instance.

    `_runtime` caches the model in a module global, so every analyzer in the
    process shares one object. Patching the instance wraps the previous
    wrapper and the counts multiply -- that mistake produced a reading of
    140,577 inferences on a 3,000-frame call before it was noticed.
    """

    def __enter__(self):
        model, _ = ant._runtime()
        self._cls = type(model)
        self._orig = self._cls.probability
        self.n = 0

        def counted(inner_self, mel):
            self.n += 1
            return self._orig(inner_self, mel)

        self._cls.probability = counted
        return self

    def __exit__(self, *exc):
        self._cls.probability = self._orig
        return False


def _speech(n_frames: int) -> bytes:
    rng = np.random.default_rng(0)
    return (rng.normal(0, 3000, RATE * FRAME_MS // 1000 * n_frames)
            ).astype(np.int16).tobytes()


def _frame(loud: bool) -> bytes:
    n = RATE * FRAME_MS // 1000
    if not loud:
        return np.zeros(n, dtype=np.int16).tobytes()
    rng = np.random.default_rng()
    return rng.normal(0, 3000, n).astype(np.int16).tobytes()


def test_a_long_pause_costs_about_one_inference():
    """One second of speech then a 1.4 s pause: 70 frames, not 70 inferences."""
    a = _analyzer()
    with _Counter() as c:
        for _ in range(50):                    # 1.0 s of speech
            a.append_audio(_frame(True), True)
        for _ in range(70):                    # 1.4 s of silence
            a.append_audio(_frame(False), False)
    assert c.n <= 4, (
        f"{c.n} encoder runs across one pause; the cache is not holding. "
        "At 50-200 ms each this stalls the frame path.")
    assert c.n >= 1, "the model never ran at all"


def test_speaking_again_invalidates_the_verdict():
    """New speech is the one event that can change the answer."""
    a = _analyzer()
    with _Counter() as c:
        for _ in range(30):
            a.append_audio(_frame(True), True)
        for _ in range(20):
            a.append_audio(_frame(False), False)
        first = c.n
        for _ in range(30):                    # he carries on
            a.append_audio(_frame(True), True)
        for _ in range(20):                    # and pauses again
            a.append_audio(_frame(False), False)
        second = c.n - first
    assert first >= 1
    assert second >= 1, (
        "the caller spoke again and the cached verdict was reused; the window "
        "changed and the model was never asked")


def test_the_cache_key_excludes_trailing_silence():
    """`_speech_secs` includes silence and grows every frame.

    Keying on it directly is why an earlier attempt still ran 32 inferences a
    turn. This asserts the property the key must have, rather than the count,
    so it keeps meaning something if the constants move.
    """
    a = _analyzer()
    for _ in range(30):
        a.append_audio(_frame(True), True)
    a.append_audio(_frame(False), False)
    key_1 = a._speech_secs - a._silence_ms / 1000.0
    total_1 = a._speech_secs
    for _ in range(20):
        a.append_audio(_frame(False), False)
    key_2 = a._speech_secs - a._silence_ms / 1000.0
    total_2 = a._speech_secs

    assert total_2 > total_1, "sanity: the buffer should have grown"
    assert abs(key_2 - key_1) < 0.01, (
        "the cache key moved while the caller was silent, so the encoder will "
        "re-run on audio it has already scored")
