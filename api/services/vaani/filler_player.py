"""Plays a Telugu filler into the gap, gated on the trained turn detector.

Where the gap actually is
-------------------------
Run 262, a clean 130s call:

    endpoint + STT   0.921s average
    LLM              0.290s
    TOTAL p50        1.297s

The LLM stopped being the problem some time ago. Two thirds of every gap is the
endpoint decision plus transcript finalisation, and it happens BEFORE the turn is
declared over. So a filler triggered on the end of the turn would mask only the
last third and leave the part the caller actually notices untouched.

Hence `VADUserStoppedSpeakingFrame`, which fires about 200ms after the caller
stops -- while endpointing is still running. That is the earliest honest signal
that they may have finished, and it is the only trigger that covers the whole
0.92s.

Firing early is the entire risk
-------------------------------
VAD reports a pause, and a pause is not the end of a turn. Speaking over a
caller who was drawing breath is a worse defect than the silence, and this
project treats it as non-negotiable: `latency_budget.yaml` caps false
interruptions at 2%, and Telugu callers on the incumbent protested at 350ms.

So the trigger is not trusted on its own. It is gated on `TeluguTurnAnalyzer`,
already trained on this agent's own 1,393 clips from 396 real calls, read at the
moment the VAD reports the pause. The gate is deliberately set ABOVE the
analyzer's own turn-ending threshold: ending a turn slightly early is recoverable
because the caller's next words still arrive, whereas talking over them is not.

Every failure path here is silence
-----------------------------------
No clip on disk, no analyzer, an unconfident analyzer, a call already in
progress, a second pause in the same turn -- all of them produce no filler and
leave the call exactly as it is today. There is no path through this processor
that makes a turn slower or louder than it currently is.
"""

from __future__ import annotations

from loguru import logger

from api.services.vaani import fillers as filler_bank
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    Frame,
    OutputAudioRawFrame,
    UserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Above the analyzer's own 0.84 turn-ending threshold. A wrong turn-end is
# recoverable; a filler on top of the caller is not.
DEFAULT_CONFIDENCE = 0.90

# 20ms at 8kHz, matching the transport's frame size so playback is smooth.
CHUNK_MS = 20


class FillerState:
    """Shared between the player and the reply filter.

    The state block asks the model to open with "సరే"/"మంచిది". When a filler
    has just said one of those, the reply's own opener has to go or the caller
    hears the same word twice. This flag is how the two processors agree on
    that, without either importing the other.
    """

    def __init__(self) -> None:
        self.played_this_turn = False

    def consume(self) -> bool:
        """True once per filler, so only the first reply chunk is trimmed."""
        played, self.played_this_turn = self.played_this_turn, False
        return played


class FillerPlayer(FrameProcessor):
    """Speaks a short filler into the silence while the reply is being built."""

    def __init__(
        self,
        *,
        state: FillerState,
        turn_analyzer=None,
        voice: str = "anushka",
        sample_rate: int = 8000,
        confidence: float = DEFAULT_CONFIDENCE,
        enabled: bool = True,
    ):
        super().__init__()
        self._state = state
        self._analyzer = turn_analyzer
        self._voice = voice
        self._sample_rate = sample_rate
        self._confidence = confidence
        self._enabled = enabled
        self._armed = False          # the caller has spoken since the last filler
        self._index = 0              # rotates, so it is not the same word every turn
        self._clips = self._load()
        if enabled and not self._clips:
            logger.info(
                "[filler] no rendered clips for voice "
                f"{voice!r} at {sample_rate}Hz -- fillers are off. "
                "Run tools/render_fillers.py to enable them."
            )

    def _load(self) -> list[tuple[str, bytes]]:
        clips = []
        for text in filler_bank.FILLERS:
            pcm = filler_bank.load_cached(text, self._voice, self._sample_rate)
            if pcm:
                clips.append((text, pcm))
        return clips

    @property
    def active(self) -> bool:
        """Whether this will ever actually speak. Used by tests and logging."""
        return bool(self._enabled and self._clips)

    def _confident(self) -> bool:
        """Does the trained detector believe the caller has finished?

        No analyzer, or an analyzer that has not scored this turn, means no
        evidence -- and no evidence means stay quiet.
        """
        p = getattr(self._analyzer, "_last_probability", None)
        return p is not None and p >= self._confidence

    def _next_clip(self) -> tuple[str, bytes]:
        clip = self._clips[self._index % len(self._clips)]
        self._index += 1
        return clip

    async def _speak(self, pcm: bytes) -> None:
        step = int(self._sample_rate * CHUNK_MS / 1000) * 2
        for i in range(0, len(pcm), step):
            await self.push_frame(
                OutputAudioRawFrame(
                    audio=pcm[i : i + step],
                    sample_rate=self._sample_rate,
                    num_channels=1,
                ),
                FrameDirection.DOWNSTREAM,
            )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # Re-arm only when the caller actually speaks, so one pause per turn can
        # produce at most one filler. Without this a long thinking pause fires
        # repeatedly and the agent chatters.
        if isinstance(frame, UserStartedSpeakingFrame):
            self._armed = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._state.played_this_turn = False

        elif isinstance(frame, VADUserStoppedSpeakingFrame) and self._armed:
            self._armed = False
            if self.active and self._confident():
                text, pcm = self._next_clip()
                self._state.played_this_turn = True
                logger.debug(f"[filler] {text!r} into the gap")
                await self.push_frame(frame, direction)
                await self._speak(pcm)
                return

        await self.push_frame(frame, direction)
