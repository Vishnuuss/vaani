"""Measures the speculation hit rate on live calls.

This is a **pass-through observer**. It never alters, drops or delays a frame,
so it is safe to leave in a production pipeline.

Why measure before building: the whole <700 ms case rests on the claim that a
speculative LLM call, fired on a stable partial prefix, is usually still valid
when the caller stops talking. That claim has never been tested against real
Telugu speech. This probe turns every real call into evidence for or against it.

Once the hit rate is known, the expensive change — actually holding the
pre-generated response and replaying it on a hit — is a decision with a number
behind it instead of a hope.
"""

from loguru import logger

from api.services.pipecat.speculation.speculator import (
    Outcome,
    SpecAction,
    SpeculationStats,
    Speculator,
)
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class SpeculationProbe(FrameProcessor):
    """Scores speculative turns from the live transcription stream."""

    def __init__(self, workflow_run_id: int | None = None, coordinator=None):
        super().__init__()
        self._speculator = Speculator()
        self._workflow_run_id = workflow_run_id
        # Sits after STT, which is the only place interim transcripts exist:
        # LLMContextAggregator consumes them and never pushes them downstream.
        self._coordinator = coordinator

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # Observe only. Any exception here must never take down a live call.
        try:
            if isinstance(frame, InterimTranscriptionFrame):
                self._on_partial(frame.text)
                await self._drive_coordinator_partial(frame.text)
            elif isinstance(frame, TranscriptionFrame):
                self._on_final(frame.text)
                if self._coordinator is not None:
                    self._coordinator.pending_final_text = frame.text
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"SpeculationProbe error (ignored): {e}")

        await self.push_frame(frame, direction)

    async def _drive_coordinator_partial(self, text: str) -> None:
        if self._coordinator is None:
            return
        try:
            await self._coordinator.on_partial(text)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"[speculation] coordinator on_partial failed: {e}")

    def _on_partial(self, text: str) -> None:
        command = self._speculator.on_partial(text)
        if command.action is SpecAction.FIRE:
            logger.debug(f"[speculation] would fire on: {command.text!r}")
        elif command.action is SpecAction.CANCEL:
            logger.debug("[speculation] cancelled — partial revised backwards")

    def _on_final(self, text: str) -> None:
        outcome = self._speculator.on_turn_end(text)
        s = self._speculator.stats
        logger.info(
            f"[speculation] {outcome.value} | "
            f"turns={s.turns} hits={s.hits} partial={s.partials} "
            f"misses={s.misses} cancels={s.cancels} "
            f"hit_rate={s.hit_rate:.0%}"
        )
        if outcome is Outcome.MISS:
            logger.debug(f"[speculation] miss on final: {text!r}")
        self._speculator.reset_turn()

    @property
    def stats(self) -> SpeculationStats:
        return self._speculator.stats
