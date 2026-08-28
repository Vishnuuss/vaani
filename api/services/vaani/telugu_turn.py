"""A turn detector that speaks Telugu.

Why this exists
---------------
Endpointing -- deciding the caller has finished -- is the largest single cost in
a turn. Measured on run 213: 0.693s of a 1.05s total, against 0.245s for the LLM
and ~0.26s for speech. Every model optimisation so far has been fighting over
the smaller share.

`LocalSmartTurnAnalyzerV3` covers 23 languages and Telugu is not one of them.
LiveKit's covers 14, Deepgram Flux 10, AssemblyAI 4-6 -- none includes it. So on
a Telugu call the analyzer rarely returns COMPLETE and the turn ends on the
silence timeout instead of on a decision. That timeout is the 0.693s.

How this one was built
----------------------
Trained on the caller's own recordings: 651 separated `user.wav` files from
production, cut into 1,393 windows and labelled from the run logs -- a window
ending where a final transcript arrived is a turn end, one ending 0.6s earlier
is mid-utterance. Split by CALL, not by clip, so a high score cannot come from
recognising a speaker or a handset.

    text-only classifier      9.0% of turns endable early
    this, on audio           33.9%

at the same safety bar: fewer than 2% of callers cut off mid-sentence. The
features it leans on are tail energy, tail ratio and energy slope -- Telugu
speakers trail off when they are finished, and that is exactly the signal a
transcript throws away, which is why the text version stalled.

Two models, no new dependency
------------------------------
The boosted-tree version reaches 43.9% against the regression's 33.9% at the
same safety bar, and was previously left on the shelf because scoring it needed
scikit-learn in the voice container. That framed the trade as recall versus a
dependency, which was wrong: a boosted forest IS a pile of thresholds and
constants, and sklearn is only the thing that FOUND them. Exported to arrays by
`tools/export_gbm_turn.py` -- verified bit-exact against sklearn's own
predictions on all 1,393 clips -- scoring is a walk down 250 short trees. A few
hundred float comparisons, on numpy, which pipecat already depends on.

So the forest is used when its file is present and the regression remains the
fallback. Both are loaded the same way and both fail the same way: to `enabled =
False`, which restores today's silence timeout exactly.

Why this is worth the trouble, measured
----------------------------------------
Run 262's endpoint times are bimodal -- 0.403s on the five turns where the
detector fired, 0.78-1.22s on the eight where it did not. Every turn moved from
the second group into the first is worth roughly half a second to the caller,
and that gap is now the whole remaining latency budget.

It can only make things faster
------------------------------
Returning COMPLETE ends the turn early. Returning INCOMPLETE changes nothing --
the existing silence timeout still fires exactly when it does today. There is no
path through this class that makes a turn slower than it already is, and if the
model fails to load, `enabled` is False and the analyzer defers entirely.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
from loguru import logger

from pipecat.audio.turn.base_turn_analyzer import (
    BaseTurnAnalyzer,
    BaseTurnParams,
    EndOfTurnState,
)

# Where `tools/train_audio_turn.py` writes the exported weights.
WEIGHTS_PATH = Path(__file__).parent / "models" / "audio_turn_weights.json"
# The boosted forest, written by `tools/export_gbm_turn.py`. Preferred when
# present; the regression above is the fallback.
GBM_PATH = Path(__file__).parent / "models" / "audio_turn_gbm.json"

# The model reads the tail of the utterance; this is how much of it.
WINDOW_S = 1.5
# Run the model once silence reaches this. Below the VAD's own stop window, so
# a verdict is available the moment the VAD would otherwise start waiting.
MIN_SILENCE_MS = 120


def _f0_track(x: np.ndarray, sr: int, frame: int = 256) -> np.ndarray:
    """Autocorrelation pitch track; zero where unvoiced.

    Only the DIRECTION matters here -- falling pitch ends a statement -- so this
    does not need to be musically accurate, and a cheap estimate keeps the whole
    feature pass in the low milliseconds.
    """
    out = []
    lo, hi = max(1, sr // 350), max(2, sr // 70)
    for i in range(0, len(x) - frame, frame):
        seg = x[i : i + frame] - x[i : i + frame].mean()
        if np.sqrt((seg**2).mean()) < 0.01:
            out.append(0.0)
            continue
        ac = np.correlate(seg, seg, mode="full")[frame - 1 :]
        band = ac[lo:hi]
        if ac[0] <= 0 or band.size == 0:
            out.append(0.0)
            continue
        peak = int(np.argmax(band)) + lo
        out.append(sr / peak if ac[peak] / ac[0] > 0.3 else 0.0)
    return np.asarray(out, dtype=np.float32)


def extract_features(x: np.ndarray, sr: int) -> list[float] | None:
    """The 16 numbers the model was trained on. Must match the trainer exactly."""
    frame = 128
    n = len(x)
    if n < frame * 6:
        return None
    energy = np.asarray(
        [float(np.sqrt((x[i : i + frame] ** 2).mean()))
         for i in range(0, n - frame, frame)]
    )
    if energy.size < 6:
        return None
    e = energy / (energy.max() + 1e-9)
    third = max(1, len(e) // 3)
    tail_n = max(1, int(0.3 * sr / frame))
    t = np.arange(len(e), dtype=np.float32)
    slope = float(np.polyfit(t, e, 1)[0])
    tail_slope = float(np.polyfit(t[-third:], e[-third:], 1)[0])

    f0 = _f0_track(x, sr)
    voiced = f0[f0 > 0]
    if voiced.size >= 4:
        vt = np.arange(voiced.size, dtype=np.float32)
        k = max(2, voiced.size // 3)
        f0_slope = float(np.polyfit(vt, voiced, 1)[0])
        f0_tail = float(np.polyfit(vt[-k:], voiced[-k:], 1)[0])
        f0_rng = float(voiced.max() - voiced.min())
        f0_mean = float(voiced.mean())
    else:
        f0_slope = f0_tail = f0_rng = f0_mean = 0.0

    half = min(len(x), sr // 2)
    spec = np.abs(np.fft.rfft(x[-half:] * np.hanning(half)))
    freqs = np.fft.rfftfreq(half, 1 / sr)
    centroid = float((spec * freqs).sum() / (spec.sum() + 1e-9))

    return [
        slope, tail_slope,
        float(e[-tail_n:].mean()), float(e[-tail_n:].mean() / (e.mean() + 1e-9)),
        float((e < 0.08).mean()), float((e[-tail_n:] < 0.08).mean()),
        float(e.std()), float(e.mean()), float(e.max()),
        f0_slope, f0_tail, f0_rng, f0_mean,
        float((f0 > 0).mean()),
        float((f0[-tail_n:] > 0).mean()) if len(f0) >= tail_n else 0.0,
        centroid,
    ]


class _Forest:
    """A gradient-boosted forest as flat arrays.

    Deliberately not a class hierarchy of nodes: four parallel lists per tree is
    exactly what sklearn holds internally, so the export is a copy rather than a
    reinterpretation, and `tools/export_gbm_turn.py` can prove the two agree to
    5.6e-16 across every clip in the training set.
    """

    __slots__ = ("trees", "lr", "init")

    def __init__(self, blob: dict):
        self.trees = [
            (np.asarray(t["feature"], dtype=np.int32),
             np.asarray(t["threshold"], dtype=np.float64),
             np.asarray(t["left"], dtype=np.int32),
             np.asarray(t["right"], dtype=np.int32),
             np.asarray(t["value"], dtype=np.float64))
            for t in blob["trees"]
        ]
        self.lr = float(blob["learning_rate"])
        self.init = float(blob["init"])

    def probability(self, x: np.ndarray) -> float:
        total = 0.0
        for feature, threshold, left, right, value in self.trees:
            node = 0
            # A negative feature index marks a leaf; this is sklearn's own
            # convention, kept so the arrays need no rewriting on export.
            while feature[node] >= 0:
                node = (left[node] if x[feature[node]] <= threshold[node]
                        else right[node])
            total += value[node]
        return float(1.0 / (1.0 + np.exp(-(self.init + self.lr * total))))


class TeluguTurnParams(BaseTurnParams):
    """Configuration for the Telugu turn analyzer."""

    stop_secs: float = 2.0          # the safety net if the model never fires
    # None means "use the value the model was trained to". An explicit number
    # WINS over the trained one -- the loader used to overwrite whatever was
    # passed in, which made the threshold impossible to override for a test or
    # for a call that wants to be more or less cautious.
    threshold: float | None = None
    max_duration_secs: float = 8.0


class TeluguTurnAnalyzer(BaseTurnAnalyzer):
    """Ends a Telugu turn on prosody instead of on a stopwatch."""

    def __init__(self, *, sample_rate: int | None = None,
                 params: TeluguTurnParams | None = None,
                 weights_path: Path | None = None,
                 gbm_path: Path | None = None):
        super().__init__(sample_rate=sample_rate)
        self._params = params or TeluguTurnParams()
        self._buffer: list[tuple[float, np.ndarray]] = []
        self._speech_triggered = False
        self._silence_ms = 0.0
        self._last_probability: float | None = None
        self._mean = self._scale = self._coef = None
        self._intercept = 0.0
        self._forest = None
        self.enabled = (self._load_forest(gbm_path or GBM_PATH)
                        or self._load(weights_path or WEIGHTS_PATH))

    def _load_forest(self, path: Path) -> bool:
        """The boosted forest, if it was exported. Never fatal."""
        try:
            blob = json.loads(Path(path).read_text(encoding="utf-8"))
            forest = _Forest(blob)
        except Exception as e:
            logger.info(f"[telugu-turn] no forest ({e.__class__.__name__}); "
                        "using the regression")
            return False
        self._forest = forest
        if self._params.threshold is None:
            self._params.threshold = float(blob.get("threshold") or 0.97)
        logger.info(
            f"[telugu-turn] forest enabled, {len(forest.trees)} trees, "
            f"threshold {self._params.threshold:.2f} "
            "(43.9% of turns endable early at a 2% false-cutoff bar)"
        )
        return True

    def _load(self, path: Path) -> bool:
        try:
            w = json.loads(Path(path).read_text(encoding="utf-8"))
            self._mean = np.asarray(w["mean"], dtype=np.float64)
            self._scale = np.asarray(w["scale"], dtype=np.float64)
            self._coef = np.asarray(w["coef"], dtype=np.float64)
            self._intercept = float(w["intercept"])
            if self._params.threshold is None and w.get("threshold"):
                self._params.threshold = float(w["threshold"])
        except Exception as e:
            # Never fatal. Without weights this analyzer simply never says
            # COMPLETE, and the turn ends the way it does today.
            logger.warning(f"[telugu-turn] disabled, no usable weights: {e!r}")
            if self._params.threshold is None:
                self._params.threshold = 1.1   # unreachable: never fires
            return False
        if self._params.threshold is None:
            self._params.threshold = 0.84     # trained default, if the file omits it
        logger.info(
            f"[telugu-turn] enabled, threshold {self._params.threshold:.2f} "
            f"(trained on 1,393 clips from 396 real calls)"
        )
        return True

    @property
    def _rate(self) -> int:
        """A usable sample rate, always.

        `BaseTurnAnalyzer` starts `_sample_rate` at 0 and only fills it in when
        the pipeline calls `set_sample_rate`. Live that happens before any audio
        arrives, but relying on ordering here means any frame that slips in
        first divides by zero and the caller's turn is dropped -- the same shape
        of failure that silenced every reply on run 213. So it is never assumed.
        """
        return self._sample_rate or self._init_sample_rate or 8000

    @property
    def params(self) -> TeluguTurnParams:
        return self._params

    @property
    def speech_triggered(self) -> bool:
        return self._speech_triggered

    def _probability(self) -> float | None:
        """How finished the caller sounds, from the tail of what they said."""
        if not self.enabled or not self._buffer:
            return None
        want = int(WINDOW_S * self._rate)
        chunks, total = [], 0
        for _, a in reversed(self._buffer):
            chunks.append(a)
            total += a.size
            if total >= want:
                break
        x = np.concatenate(list(reversed(chunks))).astype(np.float32) / 32768.0
        if x.size > want:
            x = x[-want:]
        f = extract_features(x, self._rate)
        if f is None:
            return None
        x = np.asarray(f, dtype=np.float64)
        if self._forest is not None:
            # The forest splits on RAW feature values; standardising them here
            # would silently shift every threshold in all 250 trees.
            return self._forest.probability(x)
        z = (x - self._mean) / self._scale
        return float(1.0 / (1.0 + np.exp(-(float(z @ self._coef) + self._intercept))))

    def append_audio(self, buffer: bytes, is_speech: bool) -> EndOfTurnState:
        audio = np.frombuffer(buffer, dtype=np.int16)
        self._buffer.append((time.monotonic(), audio))

        if is_speech:
            self._silence_ms = 0.0
            self._speech_triggered = True
            return EndOfTurnState.INCOMPLETE

        if not self._speech_triggered:
            # Trim so silence before the caller speaks cannot grow unbounded.
            cutoff = time.monotonic() - (self._params.max_duration_secs + WINDOW_S)
            while self._buffer and self._buffer[0][0] < cutoff:
                self._buffer.pop(0)
            return EndOfTurnState.INCOMPLETE

        self._silence_ms += audio.size / (self._rate / 1000)

        if self._silence_ms >= MIN_SILENCE_MS and self.enabled:
            p = self._probability()
            if p is not None:
                self._last_probability = p
                if p >= self._params.threshold:
                    logger.debug(
                        f"[telugu-turn] finished, p={p:.2f} after "
                        f"{self._silence_ms:.0f}ms of silence"
                    )
                    self._clear(EndOfTurnState.COMPLETE)
                    return EndOfTurnState.COMPLETE

        # The existing behaviour, untouched: if the model is not confident the
        # turn still ends on silence, exactly as it does today.
        if self._silence_ms >= self._params.stop_secs * 1000:
            self._clear(EndOfTurnState.COMPLETE)
            return EndOfTurnState.COMPLETE
        return EndOfTurnState.INCOMPLETE

    async def analyze_end_of_turn(self) -> tuple[EndOfTurnState, None]:
        p = self._probability() if self.enabled else None
        if p is not None and p >= self._params.threshold:
            self._clear(EndOfTurnState.COMPLETE)
            return EndOfTurnState.COMPLETE, None
        return EndOfTurnState.INCOMPLETE, None

    def update_vad_start_secs(self, vad_start_secs: float) -> None:
        self._vad_start_secs = vad_start_secs

    def clear(self) -> None:
        self._clear(EndOfTurnState.COMPLETE)

    def _clear(self, state: EndOfTurnState) -> None:
        self._speech_triggered = state == EndOfTurnState.INCOMPLETE
        self._buffer = []
        self._silence_ms = 0.0
