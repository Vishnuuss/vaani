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

import re
from difflib import SequenceMatcher

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

# Long enough that two genuinely different questions do not collide, short
# enough that a repeat is caught before much of it has been spoken.
_REPEAT_PREFIX = 25
# Tolerates rewording ("సుమారు" inserted mid-question) without merging two
# genuinely different questions, which share far less than this.
_REPEAT_SIMILARITY = 0.80


# The acknowledgement the state block now asks for. It is SUPPOSED to recur --
# the reference agent opens nearly every turn with one -- so it must not count
# toward "this reply is a repeat". Without stripping it, the two changes fight:
# every reply starts "అర్థమైంది సార", the repeat guard sees a match on the
# opening words and truncates the question that follows. That is exactly what
# happened on the first run, and the agent started replying "అర్థమైంది సార" and
# nothing else.
_LEADING_ACK = re.compile(
    r"^\W*(సరే(నండి|నం)?|మంచిది|మంచి\s*ఆలోచన|అర్థమైంది|అర్ధమైంది|"
    r"చాలా\s*సంతోషమండి|అలాగే(నండి)?|కరెక్టే?|ఓకే|తప్పకుండా)"
    r"[\s,.ఁ-౿]{0,12}?(సార్|అండి|మేడమ్)?\W*",
    re.IGNORECASE)


def _strip_ack(text: str) -> str:
    """Drop a leading acknowledgement, so only the substance is compared."""
    return _LEADING_ACK.sub("", (text or "").strip(), count=1)


def _normalise(text: str) -> str:
    """Punctuation and spacing are not what makes two replies different."""
    return re.sub(r"[^\wఀ-౿]+", "", (text or "").lower())
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
            self.note_user_text(frame.text)

        await self.push_frame(frame, direction)

    def note_user_text(self, text: str) -> None:
        """Run triage on one caller utterance and refresh the state block.

        Exposed as a method because not every surface delivers speech. Text chat
        queues an `LLMContextFrame` straight onto the LLM and never produces a
        `TranscriptionFrame` at all, so this processor's frame path never fired
        there -- and with it, none of the hard stops. The eval battery found that
        the hard way: told "అమ్మ ఇంట్లో లేరు, నేను చిన్న పిల్లని" (a child saying
        their mother is out) the agent asked the child for the household
        electricity bill. The pattern matched perfectly; it was simply never run.
        """
        if not (text or "").strip():
            return
        result = triage.apply(self.state, text)
        # Give the state block something concrete to acknowledge.
        self.state.last_user_text = text.strip()
        self.state.advance()
        if result.any:
            logger.info(f"triage: {result}")
        self._refresh()

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
        self._blocked = False
        # Survives across responses: repetition is a property of the CALL, not
        # of one reply, and this processor lives for the whole call.
        #
        # These two were lost in an edit and every turn after the first died
        # with "'ReplyFilter' object has no attribute '_said'" -- run 213 shows
        # fourteen pipeline errors and a caller asking "హలో, ఎందుకండీ ఇంత స్టాప్
        # అయిపోతుంది". The unit tests missed it because they build this object
        # with __new__ and set the fields by hand, so __init__ never ran.
        self._said: list[str] = []

    def _gate(self, candidate: str) -> str:
        """Judge text BEFORE it is spoken; substitute rather than log.

        The old code ran `guardrails.check` on `LLMFullResponseEndFrame` and its
        own comment admitted the problem: "the text is already on its way to TTS
        by now, so this cannot retract it". Meanwhile two docstrings claimed the
        rules ran "BEFORE a single character reaches the speech engine". They
        did not, and `SAFE_FALLBACK`, `SAFE_CLOSE` and `correction_note` sat
        unused because nothing was in a position to use them.

        Checking each chunk against everything spoken so far catches a violation
        at the moment it completes rather than after the caller has heard the
        whole reply. Only BLOCKING_RULES trigger a substitution -- cutting a
        caller off for a stray asterisk would be worse than the asterisk.
        """
        if self._blocked:
            return ""

        # Repetition is decided ONCE, on the first chunk, before a single word
        # has been spoken -- and never after.
        #
        # The earlier version checked on every chunk, so it could fire once the
        # reply was already streaming, and then all it could do was stop. The
        # eval caught the result: callers heard "అర్థమైంది బిల్లు?" and
        # "అర్థమైంది, మీరు ఇల్లు, అపార". Half a sentence is worse than the
        # repeat it was preventing.
        #
        # Nothing has been emitted while `self._spoken` is empty, so replacing
        # the reply here is a substitution rather than a truncation. The
        # sanitizer holds back 24 characters before releasing anything, which is
        # what makes that window exist at all.
        #
        # Telling the model not to repeat itself, via the state block, is kept
        # as well -- but it is not sufficient on its own. Two runs of the
        # battery repeated anyway.
        if not self._spoken and self._is_repeat(candidate):
            self._blocked = True
            logger.warning(
                f"[repeat] already asked this; saying it could not hear "
                f"instead: {candidate[:60]!r}"
            )
            return guardrails.REPAIR_LINE

        closing = bool(self._injector) and guardrails.must_close(
            self._injector.state)
        report = guardrails.check(
            self._spoken + candidate, closing=closing,
            caller_said=(self._injector.state.last_user_text
                         if self._injector else ""))
        hits = guardrails.blocking(report)
        if not hits:
            return candidate

        rules = ", ".join(f"{v.rule}({v.evidence})" for v in hits)

        # Substituting only works while NOTHING has been spoken. Once audio is
        # out, swapping the text in produces a splice, and the caller hears the
        # join: a live reply came out as
        #   "సరే, రెండు thousand rupeeసార్, కరెక్ట్ ఫిగర్ ఇప్పుడే చెప్పలేను."
        # -- the safe line grafted onto the middle of a word. That is worse than
        # the sentence it was replacing, and it is the same lesson the repeat
        # guard already taught.
        if self._spoken:
            logger.warning(
                f"[guardrail] {rules} -- already speaking, letting it finish "
                f"rather than splicing"
            )
            return candidate

        self._blocked = True
        logger.warning(f"[guardrail] reply replaced before TTS: {rules}")
        return guardrails.SAFE_CLOSE if closing else guardrails.SAFE_FALLBACK

    def _is_repeat(self, text: str) -> bool:
        """Has this call already said something this close to it?

        Similarity rather than equality, because the model rewords while asking
        the same thing. Run 96 said both
        "మీ నెలవారీ బిల్లు ఎంత రూపాయలుగా వస్తుంది?" and
        "మీ నెలవారీ బిల్లు సుమారు ఎంత రూపాయలుగా వస్తుంది?" in one call; an exact
        or prefix test calls those different, and the caller does not.

        Each previous reply is truncated to the length seen so far, so a partial
        is compared against the equivalent part of what was already spoken. The
        leading acknowledgement is stripped from both first, because it is meant
        to repeat and would otherwise make every turn look like the last one.
        """
        head = _normalise(_strip_ack(text))
        if len(head) < _REPEAT_PREFIX:
            # Either too little to judge, or the whole reply was an
            # acknowledgement -- which is meant to recur.
            return False
        for prev in self._said:
            other = _normalise(_strip_ack(prev))[: len(head)]
            if not other:
                continue
            if SequenceMatcher(None, head, other).ratio() >= _REPEAT_SIMILARITY:
                return True
        return False

    def _note_mode(self) -> None:
        """`MODE: END` is how the agent hangs up. Text chat has nothing to hang up."""
        if self._injector and self._sanitizer.mode == "END":
            self._injector.state.must_end = True

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            # One sanitizer per response; it carries per-reply truncation state.
            if self._spoken.strip():
                self._said.append(self._spoken)
                # Feed it back into the state block, which is where repetition
                # is actually prevented now.
                if self._injector is not None:
                    self._injector.state.asked.append(self._spoken.strip()[:90])
                    # The question was really put to the caller. Only now does
                    # it count against that field's two-ask budget.
                    self._injector.state.commit_ask()
            self._sanitizer = ReplySanitizer()
            self._spoken = ""
            self._blocked = False
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMTextFrame):
            speakable = self._sanitizer.feed(frame.text)
            self._note_mode()
            if not speakable:
                return
            speakable = self._gate(speakable)
            if not speakable:
                return
            frame = LLMTextFrame(speakable)
            self._spoken += speakable

        if isinstance(frame, LLMFullResponseEndFrame):
            # Always drain: the old filter had no flush here, so a short reply
            # with no trailing newline could be swallowed whole.
            tail = self._sanitizer.finish()
            self._note_mode()
            # The tail must pass the gate too. It did not, and run 93 ended with
            # the caller hearing "...మంచి రోజు సార్.all is ending: q" -- the
            # guardrail had already substituted a safe close, and then this
            # flush pushed the model's remaining text straight past it.
            tail = self._gate(tail) if tail else ""
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
            # Blocking rules were already caught and substituted in-stream by
            # `_gate`. What is left here is advisory -- markdown, over-length --
            # recorded so the offending line lands in the tuning set.
            advisory = [v for v in report.violations
                        if v.rule not in guardrails.BLOCKING_RULES]
            if advisory:
                logger.warning(
                    "guardrail (advisory): "
                    + "; ".join(f"{v.rule}({v.evidence})" for v in advisory))

        await self.push_frame(frame, direction)
