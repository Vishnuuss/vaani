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

from api.services.vaani import fillers, guardrails, triage
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
    # Greedy over PUNCTUATION AND SPACE only, so the trailing honorific is
    # actually reached. The old class spanned the whole Telugu block and had to
    # be lazy to stay safe, which meant it stopped before "అండి" and left it
    # attached to the substance: "సరే అండి, ఏ loan అండి?" stripped to
    # "అండి, ఏ loan అండి?" while a bare "ఏ loan అండి?" stripped to itself, so
    # the two never compared equal and run 721 repeated the question four times.
    r"[\s,.]{0,4}(సార్|అండి|మేడమ్)?\W*",
    re.IGNORECASE)


def _strip_ack(text: str) -> str:
    """Drop a leading acknowledgement, so only the substance is compared."""
    return _LEADING_ACK.sub("", (text or "").strip(), count=1)


def _normalise(text: str) -> str:
    """Punctuation and spacing are not what makes two replies different."""
    return re.sub(r"[^\wఀ-౿]+", "", (text or "").lower())
from api.services.vaani.state import CallState, echoes_agent


class StateInjector(FrameProcessor):
    """Keeps the live state block at the end of the LLM context."""

    def __init__(self, brief: Brief, context, system_prompt: str):
        super().__init__()
        self._context = context
        self._system_prompt = system_prompt
        # Every number the agent is ALLOWED to say, taken from its own compiled
        # prompt -- which contains the client's knowledge base. Computed once
        # per call rather than per turn: the prompt does not change mid-call,
        # and this scans ~30 KB of text.
        #
        # This is what makes the invented-quantity rule work for any client
        # without a code change: a new knowledge base defines its own legal
        # numbers just by containing them.
        self.known_numbers = guardrails.numbers_in(system_prompt)
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
        # The agent's own voice, echoed back by a speakerphone. Acting on it is
        # what turned run 270 into a call with no human content in it -- see
        # state.echoes_agent. Dropped before triage, before the state block, and
        # before it can become "what the caller said".
        if echoes_agent(text, self.state.asked):
            logger.info(f"[echo] ignoring the agent's own words: {text[:60]!r}")
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

    def __init__(self, injector: "StateInjector | None" = None,
                 filler_state=None):
        """`injector` is the voice path's call state.

        Text chat has no CallState and no phone to hang up, but it runs the same
        model on the same prompt and so produces the same malformed replies --
        the eval battery caught a two-question turn there. Passing None gives
        the sanitising half without the voice-only half, which lets one processor
        serve both paths and keeps the eval a true proxy for a call.
        """
        super().__init__()
        self._injector = injector
        # Set when a filler has just been spoken. The state block asks the model
        # to open with "సరే"/"మంచిది", and the filler has usually just said one
        # of those -- without this the caller hears the same word twice.
        self._filler_state = filler_state
        self._sanitizer = ReplySanitizer(self._caller_names())
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


    def _caller_names(self) -> tuple[str, ...]:
        """Whoever the agent is on the phone to, if it has been told yet.

        Only used to put గారు after a name instead of అండి, so an empty tuple
        early in the call is correct rather than a gap: before the name is
        known the agent has no name to get wrong.
        """
        state = getattr(self._injector, "state", None)
        name = (getattr(state, "known", {}) or {}).get("customer_name", "")
        return (str(name).strip(),) if name else ()

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
            # Whatever comes back next is an answer to a question the caller
            # has now heard twice and we have already failed to understand
            # once. It is not evidence for a disqualifier. Run 312 hung up on
            # a factory owner at exactly this point.
            if self._injector:
                self._injector.state.misheard_last_turn = True
            return guardrails.REPAIR_LINE

        # The model writing its OWN "I could not hear you" counts exactly like
        # the guard writing one: whatever comes back next is an answer to a
        # question we have already failed to understand once. Run 314 apologised
        # for not hearing the city and asked about the property instead.
        if self._injector and guardrails.SAID_NOT_HEARD.search(candidate):
            self._injector.state.misheard_last_turn = True

        closing = bool(self._injector) and guardrails.must_close(
            self._injector.state)
        report = guardrails.check(
            self._spoken + candidate, closing=closing,
            caller_said=(self._injector.state.last_user_text
                         if self._injector else ""),
            # getattr, not attribute access: the injector is a test double in
            # several suites, and a missing whitelist must mean "do not enforce"
            # rather than an AttributeError on a live call.
            known_numbers=getattr(self._injector, "known_numbers", None))
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
        if not head:
            # The whole reply was an acknowledgement, which is meant to recur.
            return False

        # Everything this call has already said, including earlier TURNS.
        #
        # `self._said` lives on this object, and on a phone call that object
        # lives for the whole call. Text chat rebuilds the pipeline for every
        # message (`text_chat_runner`), so `_said` was always empty there and
        # the guard was blind -- run 721 asked one question four times with the
        # ask budget already correctly capped at two. `state.asked` is
        # persisted across turns, so it is the half that survives a rebuild.
        previous = list(self._said)
        state = getattr(self._injector, "state", None)
        if state is not None:
            previous.extend(getattr(state, "asked", []) or [])

        for prev in previous:
            other = _normalise(_strip_ack(prev))
            if not other:
                continue
            # An EXACT repeat is unambiguous at any length. The length floor
            # below exists so two different SHORT questions are not called the
            # same by a fuzzy ratio; it was never a reason to allow a sentence
            # to be said twice word for word. wf3's "ఏ loan అండి?" is twelve
            # characters and was repeating under that floor.
            if other == head:
                return True
            if len(head) < _REPEAT_PREFIX:
                continue
            if SequenceMatcher(None, head, other[: len(head)]).ratio() >= _REPEAT_SIMILARITY:
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
                    said = self._spoken.strip()[:90]
                    if said not in self._injector.state.asked:
                        self._injector.state.asked.append(said)
                    # The question was really put to the caller. Only now does
                    # it count against that field's two-ask budget.
                    self._injector.state.commit_ask()
            # Rebuilt per reply on purpose: the caller's name is
            # usually learned halfway through the call, so it is
            # read fresh rather than captured at construction.
            self._sanitizer = ReplySanitizer(self._caller_names())
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
            # A filler has just said "సరే"; saying it again is the agent
            # stammering. Only the first chunk of the reply is trimmed, and only
            # when a filler actually played.
            if not self._spoken and self._filler_state is not None                     and self._filler_state.consume():
                speakable = fillers.strip_leading_ack(speakable)
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

            # The reply is finished, so the question in it was really put to the
            # caller. Counted HERE as well as on the next response's start
            # frame, because on a text-chat turn there is no next start frame:
            # `text_chat_runner` builds one pipeline per message, so this object
            # is discarded before the frame that used to do the counting ever
            # arrives. Every text-chat turn was therefore turn one, and
            # MAX_ASKS_PER_FIELD could never bite.
            #
            # Safe to do both. `commit_ask` clears `pending_ask`, so the start
            # frame finds nothing left to charge -- the second call is a no-op
            # rather than a double count. `asked` is guarded the same way.
            if self._injector is not None and self._spoken.strip():
                said = self._spoken.strip()[:90]
                if said not in self._injector.state.asked:
                    self._injector.state.asked.append(said)
                self._injector.state.commit_ask()

        await self.push_frame(frame, direction)
