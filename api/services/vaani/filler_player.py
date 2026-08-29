"""Cover the gap continuously, until the real answer is ready to speak.

What the first version got wrong
--------------------------------
It played ONE clip at the VAD stop and then stopped. Run 267's recording shows
the result -- 30 audio bursts against 18 sentences, and between a filler and the
sentence it was meant to introduce, 1.0 to 1.8 seconds of silence:

    36.80s  filler 0.17s          113.52s  filler 0.37s
    37.80s  the sentence          115.34s  the sentence

The caller heard a stray word, a second of nothing, then the answer. Worse than
the silence alone: silence reads as thinking, while a word followed by silence
reads as a machine that has lost its place. The client heard it on the first
call and called it "that aaa in the middle".

The premise was right and the implementation was backwards. A filler is not a
marker dropped at the start of a gap. It is cover held until the gap ends.

So this streams, paced in real time, and stops the instant the reply's first
audio appears -- speech from ~0.2s after the caller stops, continuously, into
the answer, with no seam and no dead air in the middle.

Pacing IS the design. Queueing a whole clip pushes the reply out behind it and
adds latency, which is precisely what the first version did. One chunk every
20ms means the reply is never more than a single chunk behind the moment it is
ready, and the cover stops mid-word when it arrives.

Nothing here makes the answer come sooner. Endpoint plus STT is 0.72s and the
LLM another 0.28s; that information cannot arrive faster. What changes is when
the caller first hears a voice -- about 1.0s to about 0.2s -- which is the
number they actually experience.


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

import asyncio
import time

from loguru import logger

from api.services.vaani import fillers as filler_bank
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    Frame,
    OutputAudioRawFrame,
    InterruptionFrame,
    TTSAudioRawFrame,
    UserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Above the analyzer's own 0.84 turn-ending threshold. A wrong turn-end is
# recoverable; a filler on top of the caller is not.
DEFAULT_CONFIDENCE = 0.90

# One transport frame, and also the pacing interval: emit 20ms, wait 20ms.
CHUNK_MS = 20

# Stop covering after this. If the reply has not arrived by now something is
# wrong upstream, and a caller listening to an agent that will not stop
# murmuring is worse off than one hearing silence.
MAX_COVER_S = 3.0

# A breath between clips, so the cover sounds like someone thinking rather than
# a loop. Short enough that it never reads as the gap this exists to remove.
GAP_MS = 220

# How far ahead of real time the cover is allowed to run. This is precisely how
# long the real reply can sit behind the cover once it is ready, so it is kept
# to three frames -- enough that the transport never starves, small enough that
# it cannot become the latency it was added to hide.
LEAD_MS = 60


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
        self._index = 0              # rotates, so it is not the same word twice
        self._task: asyncio.Task | None = None
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

    async def _cover(self) -> None:
        """Speak, paced against the clock, until the real reply cancels this.

        Paced against a monotonic clock rather than by sleeping a fixed interval
        per chunk. `asyncio.sleep(0.02)` does not take 20ms -- on Windows it is
        nearer 35 -- so a fixed sleep emits audio slower than real time and the
        transport underruns, which the caller hears as chopping. Measured: 0.50s
        of audio produced over a 1.0s gap.

        A small lead is kept so the transport always has something to play. It
        is capped at LEAD_MS because that lead is exactly how long the real reply
        can be stuck behind the cover once it is ready: the thing this feature
        exists to remove.
        """
        step = int(self._sample_rate * CHUNK_MS / 1000) * 2
        started = time.monotonic()
        spoken = 0.0                     # seconds of audio emitted so far
        while spoken < MAX_COVER_S:
            text, pcm = self._next_clip()
            logger.debug(f"[filler] covering with {text!r}")
            for i in range(0, len(pcm), step):
                elapsed = time.monotonic() - started
                ahead = spoken - elapsed
                if ahead >= LEAD_MS / 1000:
                    await asyncio.sleep(ahead - LEAD_MS / 2000)
                await self.push_frame(
                    OutputAudioRawFrame(
                        audio=pcm[i:i + step],
                        sample_rate=self._sample_rate,
                        num_channels=1,
                    ),
                    FrameDirection.DOWNSTREAM,
                )
                spoken += CHUNK_MS / 1000
                if spoken >= MAX_COVER_S:
                    return
            # A breath between words, so it reads as thinking, not a loop.
            await asyncio.sleep(GAP_MS / 1000)
            spoken += GAP_MS / 1000

    async def _stop_cover(self) -> None:
        """Hand over to the real reply. Stopping mid-word is correct."""
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                # Cancellation is the expected outcome here, and a cover that
                # fails on its way out must never surface on the call path.
                pass

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # The real reply has audio. Hand over immediately -- this is the seam
        # the whole design is built around.
        if isinstance(frame, (TTSAudioRawFrame, InterruptionFrame)):
            await self._stop_cover()

        # Re-arm only when the caller actually speaks, so one pause per turn can
        # produce at most one cover. Without this a long thinking pause fires
        # repeatedly and the agent chatters.
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._armed = True
            await self._stop_cover()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._state.played_this_turn = False
            await self._stop_cover()

        elif isinstance(frame, VADUserStoppedSpeakingFrame) and self._armed:
            self._armed = False
            if self.active and self._confident() and self._task is None:
                self._state.played_this_turn = True
                await self.push_frame(frame, direction)
                self._task = asyncio.create_task(self._cover())
                return

        await self.push_frame(frame, direction)
