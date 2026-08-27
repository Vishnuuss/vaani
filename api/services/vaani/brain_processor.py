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

ReplyFilter    sanitises the reply before a character reaches the speech engine.
               The stripping logic lives in `reply_sanitizer`, which documents
               why a first-line-only header strip was not enough: run 12 put
               `MODE: CLOSE` in the middle of a reply and the caller heard it.

               Because this sits upstream of the assistant context aggregator,
               the cleaned text is also what lands in the conversation history --
               which is what stops one malformed turn from teaching the next.
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
from api.services.vaani.reply_sanitizer import ReplySanitizer
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
    """Sanitises the reply and enforces the hard rules before TTS."""

    def __init__(self, injector: "StateInjector | None" = None):
        """`injector` is the voice path's call state.

        Text chat has no CallState and no phone to hang up, but it runs the same
        model on the same prompt and so produces the same malformed replies --
        the eval battery caught a two-question turn there. Passing None gives
        the sanitising half without the voice-only half, which lets one processor
        serve both paths and keeps the eval a true proxy for a call.
        """
        super().__init__()
        self._injector = injector
        self._sanitizer = ReplySanitizer()
        self._spoken = ""

    def _note_mode(self) -> None:
        """`MODE: END` is how the agent hangs up. Text chat has nothing to hang up."""
        if self._injector and self._sanitizer.mode == "END":
            self._injector.state.must_end = True

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            # One sanitizer per response; it carries per-reply truncation state.
            self._sanitizer = ReplySanitizer()
            self._spoken = ""
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMTextFrame):
            speakable = self._sanitizer.feed(frame.text)
            self._note_mode()
            if not speakable:
                return
            frame = LLMTextFrame(speakable)
            self._spoken += speakable

        if isinstance(frame, LLMFullResponseEndFrame):
            # Always drain: the old filter had no flush here, so a short reply
            # with no trailing newline could be swallowed whole.
            tail = self._sanitizer.finish()
            self._note_mode()
            if tail:
                self._spoken += tail
                await self.push_frame(LLMTextFrame(tail), direction)
            if self._sanitizer.removed:
                logger.warning(
                    "[reply] removed from the spoken reply: "
                    + repr("".join(self._sanitizer.removed))[:300]
                )
            report = guardrails.check(
                self._spoken,
                closing=bool(self._injector)
                and guardrails.must_close(self._injector.state))
            if not report.ok:
                # The text is already on its way to TTS by now, so this cannot
                # retract it -- it is recorded so the offending line lands in
                # the tuning set instead of disappearing.
                logger.warning(
                    "guardrail violation: "
                    + "; ".join(f"{v.rule}({v.evidence})" for v in report.violations))

        await self.push_frame(frame, direction)
