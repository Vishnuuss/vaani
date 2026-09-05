"""Take the STT's finalisation off the latency critical path.

The measurement
---------------
Run 3, a real Vobiz call on Sarvam `saarika:v2.5`:

    turn   TOTAL   endpoint+STT     LLM   LLM->audio
    2      1.815s        1.260s   0.100s      0.455s
    3      2.058s        1.317s   0.106s      0.635s
    4      1.980s        1.448s   0.093s      0.439s
    5      1.582s        1.314s   0.096s      0.172s
    6      2.125s        1.322s   0.093s      0.710s
    avg    1.912s        1.332s   0.098s      0.482s

70% of the turn is spent before the LLM is even asked. The same agent on
Deepgram averaged 0.7s there. Sarvam is not slower at *recognising* -- run 3 is
the first call that ever understood the caller ("5 టు 7000", "హైదరాబాద్
అనంతపూర్", extracted correctly). It is slower to declare a transcript FINAL,
and we were holding the turn until it did.

`bench/FINDINGS.md` §5 measured 438ms p50 from flush to final on 2026-08-25 and
stated the fix plainly: *"respond off the partial, never wait for
transcript.final."* It was never built. Confirmed by grep: on the live path
`InterimTranscriptionFrame` reaches only observers -- the realtime feedback
observer and the speculation probe. `LLMContextAggregator` consumes interim
frames and never pushes them downstream, so no partial has ever reached the LLM.

What this does
--------------
Sits after STT and before the user aggregator -- the only window where interim
frames exist. It watches partials, and when the turn ends with no final in hand
it promotes the newest partial to a real `TranscriptionFrame` so the LLM starts
immediately. The genuine final, when it arrives, is suppressed: the aggregator
would otherwise treat it as a second utterance and run the turn twice on the
same speech.

Why the NEWEST partial and not the stable prefix
------------------------------------------------
Partials are not monotonic -- Sarvam revises backwards as it re-decodes -- so
the instinct is to promote only the prefix two consecutive partials agreed on.
That is right for firing speculatively mid-utterance; it is wrong here.

At turn end the caller has stopped and no further audio is coming, so the
newest partial is the decoder's last word on the utterance. Preferring the
agreed prefix destroys short answers, which is nearly all of this agent's
traffic: "five thousand" arrives as ["five", "five thousand"], whose agreed
prefix is "five" -- the LLM would be told the bill is 5. A test caught exactly
that.

Safety
------
The trade is explicit: the LLM may see text a word or two short of what the
caller finally said. `user_turn_stop_timeout` remains the safety net, and when
the STT is fast enough to deliver a final before the turn ends, this class does
nothing at all and behaviour is exactly as before.
"""

from __future__ import annotations

from loguru import logger

from api.services.pipecat.speculation.stable_prefix import StablePrefixTracker
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class PartialResponder(FrameProcessor):
    """Promotes the newest partial to a transcript when the turn ends first."""

    def __init__(self) -> None:
        super().__init__()
        self._reset_turn()

    def _reset_turn(self) -> None:
        self._tracker = StablePrefixTracker()
        self._stable = ""
        self._latest = ""
        self._have_final = False
        self._promoted = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        try:
            if isinstance(frame, InterimTranscriptionFrame):
                self._on_partial(frame.text)

            elif isinstance(frame, TranscriptionFrame):
                if self._promoted:
                    # We already answered this utterance off the partial. Letting
                    # the real final through would make the aggregator run a
                    # second turn on speech the caller only said once.
                    logger.debug(
                        f"[partial] suppressing late final {frame.text!r} "
                        f"(already answered {self._stable!r})"
                    )
                    self._reset_turn()
                    return
                self._have_final = True

            elif isinstance(frame, UserStoppedSpeakingFrame):
                promoted = self._promote(frame)
                # The event carries on the way it was going; the aggregator's
                # own bookkeeping depends on that.
                await self.push_frame(frame, direction)
                if promoted is not None:
                    # The transcript ALWAYS goes downstream, whatever direction
                    # the turn-end event arrived in.
                    #
                    # `UserStoppedSpeakingFrame` is not produced by the
                    # transport. Its only live emitter is the user aggregator,
                    # via `broadcast_frame`, which pushes one copy downstream
                    # and one UPSTREAM. This processor sits BEFORE the
                    # aggregator in `vaani/pipeline.py`, so the only copy it
                    # ever receives is the upstream one -- and it was pushing
                    # the promoted transcript back that way too, toward the STT,
                    # where nothing consumes it. The LLM never saw it.
                    #
                    # It also sets `_promoted`, which suppresses the genuine
                    # final arriving next, so the turn would have ended with the
                    # agent holding no text at all.
                    #
                    # Every existing test drives DOWNSTREAM, which is the one
                    # direction this branch never sees live.
                    await self.push_frame(promoted, FrameDirection.DOWNSTREAM)
                return

        except Exception as e:  # a diagnostics path must never drop a call
            logger.warning(f"[partial] error ignored: {e}")

        await self.push_frame(frame, direction)

    def _on_partial(self, text: str) -> None:
        self._latest = text
        result = self._tracker.observe(text)
        if result.stable_prefix:
            self._stable = result.stable_prefix

    def _promote(self, turn_end_frame: Frame) -> TranscriptionFrame | None:
        """Return the transcript to inject, or None to change nothing."""
        if self._promoted:
            # AT MOST ONCE PER TURN.
            #
            # `_promoted` was only ever read on the TranscriptionFrame path, to
            # suppress the late final. Nothing stopped a SECOND turn-end event
            # from promoting the same stale `_latest` again -- and with a
            # realtime STT there are many turn-end events per utterance, so the
            # same words would be injected repeatedly. Run 780 is what that
            # looks like from the outside: "హలో" twenty-two times.
            return None
        if self._have_final:
            self._reset_turn()
            return None

        # Use the NEWEST partial, not the two-partials-agree prefix.
        #
        # The stable-prefix rule exists to decide when it is safe to fire
        # SPECULATIVELY, mid-utterance, while the caller is still talking and
        # the decoder may still contradict itself. At turn end that risk is
        # gone: the caller has stopped, and no further audio is coming, so the
        # newest partial is the decoder's best and last word on the utterance.
        #
        # Preferring the stable prefix here actively destroys short answers,
        # which is what this agent almost always receives. "five thousand"
        # arrives as partials ["five", "five thousand"]; their agreed prefix is
        # just "five", so the LLM would be told the bill is 5. A test caught
        # exactly that. Qualification callers answer in two words -- there is
        # rarely time for any word to be confirmed twice.
        #
        # `_stable` remains the fallback for the case where the last partial
        # arrives empty.
        text = (self._latest or self._stable).strip()
        if not text:
            self._reset_turn()
            return None

        logger.info(f"[partial] turn ended with no final -- responding off {text!r}")
        self._promoted = True
        return TranscriptionFrame(text, "", "")
