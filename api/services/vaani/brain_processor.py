"""The brain, on the live audio path.

Everything the simulator proved has to actually run during a phone call, or the
tuning was theatre. This is that wiring, and it is deliberately the same code
the simulator exercises -- `triage`, `CallState`, `guardrails`, `parse_mode` --
so a gate pass means something about production.

Two processors, placed on either side of the LLM:

    stt -> StateInjector -> aggregator.user() -> llm -> ReplyFilter -> tts

StateInjector  runs synchronous triage on what the caller just said and rewrites
               the trailing system message with the fresh state block. It sits
               BEFORE the aggregator so the state is current when the LLM fires.

ReplyFilter    strips the MODE line before a single character reaches the speech
               engine, and blocks a reply that breaks a hard rule.

Why the MODE line needs stripping carefully: the LLM streams, so "MODE: ASK"
arrives as several tiny text frames before the real sentence. Passing them
through would have the agent literally say "mode ask" out loud. So the filter
holds text back until the first newline has gone by.
"""

from __future__ import annotations

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from api.services.vaani import guardrails, triage
from api.services.vaani.compiler import MODE_PROTOCOL, Brief
from api.services.vaani.state import CallState


class StateInjector(FrameProcessor):
    """Keeps the live state block at the end of the LLM context."""

    def __init__(self, brief: Brief, context, system_prompt: str):
        super().__init__()
        self._context = context
        self._system_prompt = system_prompt
        self.state = CallState(
            required_fields=brief.field_names,
            questions=dict(zip(brief.field_names, brief.question_texts)),
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # Only final transcripts move the call on. Interim ones revise
        # backwards and would make triage flap.
        if isinstance(frame, TranscriptionFrame) and (frame.text or "").strip():
            result = triage.apply(self.state, frame.text)
            self.state.advance()
            if result.any:
                logger.info(f"triage: {result}")
            self._refresh()

        await self.push_frame(frame, direction)

    def _refresh(self) -> None:
        """Rebuild the context so the state block is last, and therefore loudest."""
        messages = [m for m in self._context.messages
                    if m.get("role") != "system"
                    or m.get("content") not in (self._system_prompt,)]
        # Drop any previous state block we appended; it is stale now.
        messages = [m for m in messages
                    if not (m.get("role") == "system"
                            and "STILL_NEED" in str(m.get("content", "")))]
        self._context.set_messages(
            [{"role": "system", "content": self._system_prompt}]
            + messages
            + [{"role": "system",
                "content": self.state.render() + MODE_PROTOCOL}]
        )


class ReplyFilter(FrameProcessor):
    """Strips the MODE line and enforces the hard rules before TTS."""

    def __init__(self, injector: StateInjector):
        super().__init__()
        self._injector = injector
        self._buffer = ""
        self._mode_done = False
        self._spoken = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._buffer, self._mode_done, self._spoken = "", False, ""
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMTextFrame):
            if not self._mode_done:
                self._buffer += frame.text
                # Hold everything until the MODE line has definitely ended.
                if "\n" not in self._buffer:
                    if len(self._buffer) < 40:      # still plausibly the header
                        return
                    self._mode_done = True          # model skipped the line
                else:
                    head, _, rest = self._buffer.partition("\n")
                    if head.strip().upper().startswith("MODE:"):
                        mode = head.split(":", 1)[1].strip().upper()
                        if mode.startswith("END"):
                            self._injector.state.must_end = True
                        logger.info(f"mode={mode}")
                        self._buffer = rest.lstrip("\n")
                    self._mode_done = True
                if not self._buffer:
                    return
                frame = LLMTextFrame(self._buffer)
                self._buffer = ""
            self._spoken += frame.text

        if isinstance(frame, LLMFullResponseEndFrame):
            report = guardrails.check(
                self._spoken,
                closing=guardrails.must_close(self._injector.state))
            if not report.ok:
                # The text is already on its way to TTS by now, so this cannot
                # retract it -- it is recorded so the offending line lands in
                # the tuning set instead of disappearing.
                logger.warning(
                    "guardrail violation: "
                    + "; ".join(f"{v.rule}({v.evidence})" for v in report.violations))

        await self.push_frame(frame, direction)
