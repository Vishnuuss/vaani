"""Takes the LLM off the critical path when a speculation hits.

Placed immediately before the LLM service. Two jobs:

* feed interim transcripts to the coordinator, so generation can start while
  the caller is still speaking;
* when the turn ends, if the coordinator holds a response generated from
  exactly the text the caller said, emit it as a normal LLM response and
  **swallow the trigger frame** so the real LLM never runs.

On a miss — or on any error — the trigger frame passes straight through and the
pipeline behaves exactly as it does without this gate. A speculation failure
must never cost the caller their turn.
"""

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# The frames that tell an LLM service to generate.
_TRIGGER_FRAMES = (LLMRunFrame, LLMContextFrame)


class SpeculativeLLMGate(FrameProcessor):
    """Replays a pre-generated response instead of calling the LLM."""

    def __init__(self, coordinator):
        super().__init__()
        self._coordinator = coordinator
        self._last_user_text = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InterimTranscriptionFrame):
            await self._safe_partial(frame.text)
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame):
            self._last_user_text = frame.text
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, _TRIGGER_FRAMES):
            if await self._try_replay(direction):
                return  # trigger swallowed; the real LLM never sees it

        await self.push_frame(frame, direction)

    async def _safe_partial(self, text: str) -> None:
        try:
            await self._coordinator.on_partial(text)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"[speculation] on_partial failed (ignored): {e}")

    async def _try_replay(self, direction: FrameDirection) -> bool:
        try:
            final_text = (
                getattr(self._coordinator, "pending_final_text", "")
                or self._last_user_text
            )
            tokens = await self._coordinator.take_response_for(final_text)
        except Exception as e:
            logger.warning(f"[speculation] take_response failed, using real LLM: {e}")
            return False

        if not tokens:
            return False

        logger.info("[speculation] replaying pre-generated response — LLM skipped")
        await self.push_frame(LLMFullResponseStartFrame(), direction)
        for token in tokens:
            await self.push_frame(LLMTextFrame(token), direction)
        await self.push_frame(LLMFullResponseEndFrame(), direction)
        return True
