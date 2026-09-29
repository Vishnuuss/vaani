"""TurnSense analyzer: end the turn when the WORDS say he has finished.

How the wait is decided
-----------------------
Silero stops (0.2 s after the last word). pipecat sends Soniox a finalize and
the pause's text arrives ~0.25 s later; `note_text` scores it with
`turnsense.TurnSenseModel` -> p = P(finished). Then:

    p >= fast_p           end NOW, at the text             (~0.45 s after his last word)
    slow_p < p < fast_p   wait, interpolated toward max
    p <= slow_p           wait max_wait_secs of silence    (he is mid-sentence)
    no text for the pause end at no_text_wait_secs       (today's 0.7 s timer -- never worse)

Any speech resets everything, exactly as the timer does today: the caller always
keeps the floor while he is talking, and every wait here is a wait for SILENCE.

Why it cannot be worse than the timer where it does not know
------------------------------------------------------------
The no-text branch IS the live timer (0.7 s after Silero's stop). The model only
moves a pause off it when it has the pause's own words in hand -- never on the
previous utterance's text, the defect that made the old analyzer's text rule
read the wrong sentence on 69% of decisions (Sep 7: text fresh 31.2%).

The agent's last line is context ("మీ పేరు?" makes "విష్ణు" finished). The
brain calls `note_agent` with every reply it speaks; without it the model runs
on the caller's words alone, which is measured separately.

Config-gated (`turn_model: turnsense` on the `turn_analyzer` strategy) and OFF
by default. Revert = set `turn_stop_strategy` back to `transcription`.
"""
from __future__ import annotations

import time

from loguru import logger
from pipecat.audio.turn.base_turn_analyzer import (
    BaseTurnAnalyzer,
    BaseTurnParams,
    EndOfTurnState,
)

from api.services.vaani.turnsense import TurnSenseModel, hold_rate

RESUME_WINDOW_S = 1.0
VAD_STOP_S = 0.2          # Silero's stop hold in production (VADParams)
HOLD_GAP_S = 1.5          # he "carried on" if he spoke again within this of his last word


class TurnSenseParams(BaseTurnParams):
    # The operating point chosen on 145 real calls, 5-fold out-of-sample
    # (docs/voice-platform/42): 5.7% cut off, 0.46 s median wait, p90 1.44 s.
    fast_p: float = 0.85
    slow_p: float = 0.60
    # Silence AFTER Silero's stop. Add 0.2 s for the time since the last word.
    min_wait_secs: float = 0.0
    mid_wait_secs: float = 0.9
    max_wait_secs: float = 1.6
    no_text_wait_secs: float = 0.7
    # Hard cap on any wait, whatever the model says.
    ceiling_secs: float = 2.0


class TurnSenseAnalyzer(BaseTurnAnalyzer):
    def __init__(self, *, sample_rate: int | None = None,
                 params: TurnSenseParams | None = None,
                 model: TurnSenseModel | None = None):
        super().__init__(sample_rate=sample_rate)
        self._params = params or TurnSenseParams()
        self._model = model if model is not None else TurnSenseModel.load()
        self.enabled = self._model is not None
        if not self.enabled:
            logger.warning("[turnsense] model artifact missing -- behaving as "
                           "the plain timer")
        self._agent = ""
        self._reset_turn()
        self._ended_at: float | None = None
        self._cutoffs = 0
        self.last_decision: dict | None = None
        # This caller's pausing habit, learned online from pauses whose outcome
        # has already happened. Survives turn ends; one analyzer per call.
        self._holds = 0
        self._pauses = 0
        self._since_stop: float | None = None
        self._was_speech = False

    # -- state ------------------------------------------------------------

    def _reset_turn(self):
        self._speech_triggered = False
        self._in_speech = False
        self._silence = 0.0
        self._seg_speech = 0.0
        self._texts: list[str] = []
        self._fresh = False
        self._p: float | None = None

    @property
    def speech_triggered(self) -> bool:
        return self._speech_triggered

    @property
    def params(self) -> TurnSenseParams:
        return self._params

    @property
    def _rate(self) -> int:
        return self._sample_rate or self._init_sample_rate or 8000

    # -- inputs -----------------------------------------------------------

    def note_agent(self, text: str) -> None:
        """The agent's line the caller is answering (context for the model)."""
        self._agent = text or ""

    def note_text(self, text: str) -> None:
        """A FINAL transcript. Fresh only if it lands in the silence it describes."""
        text = (text or "").strip()
        if not text or not self._speech_triggered:
            return
        self._texts.append(text)
        if self._in_speech:
            return                      # he is already talking again
        self._fresh = True
        if self.enabled:
            self._p = self._model.probability(
                self._agent, " ".join(self._texts), text,
                seg_secs=self._seg_speech, n_segs=len(self._texts),
                caller_hold_rate=hold_rate(self._holds, self._pauses),
                caller_pauses=self._pauses)

    # -- the decision -----------------------------------------------------

    def wait_for(self, p: float | None) -> float:
        P = self._params
        if p is None:
            return P.no_text_wait_secs
        if p >= P.fast_p:
            return P.min_wait_secs
        if p <= P.slow_p:
            return P.max_wait_secs
        frac = (p - P.slow_p) / max(1e-6, P.fast_p - P.slow_p)
        return P.max_wait_secs - frac * (P.max_wait_secs - P.mid_wait_secs)

    def append_audio(self, buffer: bytes, is_speech: bool) -> EndOfTurnState:
        dt = (len(buffer) // 2) / self._rate
        if is_speech:
            if self._since_stop is not None:
                self._pauses += 1
                if self._since_stop + VAD_STOP_S <= HOLD_GAP_S:
                    self._holds += 1
                self._since_stop = None
            self._was_speech = True
            if not self._speech_triggered and self._ended_at is not None:
                if time.monotonic() - self._ended_at < RESUME_WINDOW_S:
                    self._cutoffs += 1
                self._ended_at = None
            if not self._in_speech:
                self._seg_speech = 0.0
            self._speech_triggered = True
            self._in_speech = True
            self._silence = 0.0
            self._fresh = False
            self._p = None
            self._seg_speech += dt
            return EndOfTurnState.INCOMPLETE
        if self._was_speech:
            self._since_stop = 0.0
            self._was_speech = False
        if self._since_stop is not None:
            self._since_stop += dt
        if not self._speech_triggered:
            return EndOfTurnState.INCOMPLETE
        self._in_speech = False
        self._silence += dt
        need = self.wait_for(self._p if self._fresh else None)
        need = min(need, self._params.ceiling_secs)
        if self._silence + 1e-9 >= need:
            self.last_decision = {"p": self._p if self._fresh else None,
                                  "silence": round(self._silence, 3),
                                  "fresh": self._fresh,
                                  "holds": self._holds, "pauses": self._pauses}
            # One line per turn end, so a live call can be read decision by
            # decision: was it the words (fresh, p) or the fallback timer?
            logger.info(
                f"[turnsense] end: p={self._p if self._fresh else None} "
                f"after {self._silence:.2f}s silence "
                f"({'words' if self._fresh else 'no text -> timer'}), "
                f"caller habit {self._holds}/{self._pauses}")
            self._complete()
            return EndOfTurnState.COMPLETE
        return EndOfTurnState.INCOMPLETE

    def _complete(self):
        self._ended_at = time.monotonic()
        self._reset_turn()

    async def analyze_end_of_turn(self):
        # Asked at Silero's stop. The text for this pause is not here yet, so
        # the honest answer is "not yet": append_audio ends the turn the frame
        # after the words arrive.
        return EndOfTurnState.INCOMPLETE, None

    def clear(self) -> None:
        self._complete()
