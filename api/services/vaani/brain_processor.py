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

import json
import re
from difflib import SequenceMatcher

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
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
from api.services.vaani.state import CallState, _is_question, echoes_agent


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
        # Give the state block something concrete to acknowledge -- the WHOLE
        # turn, not the last fragment of it. Run 882: assigning per
        # transcription let his own "హలో" erase the question he was waiting on,
        # so the answer-first branch never fired. See `note_user_said`.
        self.state.note_user_said(text)
        # Before the reply is built, not after: the extractor is async and
        # lands a turn late, so without this the state block still lists the
        # field he just answered and the model dutifully asks again. Run 853.
        self.state.note_answer_to_last_ask(text)
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


def _end_is_earned(state) -> bool:
    """Does this call have a reason to be over? See `_note_mode` for run 880.

    Deliberately generous -- every genuine ending is on this list, so the guard
    only ever catches the case nothing else explains. Being wrong in this
    direction costs one more turn of conversation; being wrong in the other
    direction hangs up on a customer.
    """
    if getattr(state, "must_end", False):
        return True                                # he asked to hang up

    # He asked us something on this very turn. Run 882: "మీరు ఎక్కడి నుంచి?" --
    # where are you calling from -- answered with "call us whenever you like,
    # thank you", and the call was over. Run 880's guard did not catch it,
    # because by then he had agreed to the visit and `next_step_agreed` made
    # the ending "earned" while he was mid-question.
    #
    # Agreeing to a site survey is not agreeing to stop talking. A question on
    # the line is a caller still engaged.
    if _is_question(getattr(state, "last_user_text", "") or ""):
        return False

    return bool(
        getattr(state, "disqualified", False)
        or getattr(state, "refusals", 0)           # he said no
        or getattr(state, "no_more_questions", False)
        or getattr(state, "next_step_agreed", False)
        or getattr(state, "appointment_iso", "")   # the business is done
        or not getattr(state, "still_need", [])    # nothing left to ask
    )


class ReplyFilter(FrameProcessor):
    """Sanitises the reply and enforces the hard rules before TTS."""

    # CLASS-level defaults, deliberately, and not only belt-and-braces.
    #
    # Several tests build this object with `__new__` and set the fields they
    # care about by hand, so `__init__` never runs for them. That is how run
    # 213 shipped: `_said` was added in `__init__` alone, every turn after the
    # first died with "'ReplyFilter' object has no attribute '_said'", and the
    # unit tests could not see it. Adding state here without a class default
    # reproduces that exactly -- and it did, for thirteen tests, before this.
    #
    # False is also the right value on its own terms: an object that has not
    # been told the caller is speaking must assume he is not, or it would mute
    # every reply.
    _user_speaking = False
    _stale = False
    # True between LLMFullResponseStartFrame and its End. Class-level, because
    # several suites build this object with __new__ and an attribute added
    # without a default took out every turn after the first once already
    # (run 213, AttributeError).
    _generating = False
    # Same reasoning as the two above, and the same history: several suites
    # build this object with `__new__` and never run `__init__`, so anything
    # only set there is missing on a live method call. `_said` was added that
    # way once and every turn after the first died with an AttributeError
    # (run 213). Empty is also correct on its own terms -- nothing held back.
    _pending_repeat = ""

    def __init__(self, injector: "StateInjector | None" = None,
                 filler_state=None, in_flight=None):
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
        # Shared with the turn guard so a second caller final, arriving while
        # this reply is still being built and unheard, merges into one turn
        # instead of earning a reply of its own. Run 889.
        self._in_flight = in_flight
        self._sanitizer = ReplySanitizer(self._caller_names())
        self._spoken = ""
        self._blocked = False
        # Text held back because it MIGHT be the opening of a repeat and there
        # is not yet enough of it to tell. Always flushed -- on the next
        # decidable chunk, or at the end of the reply. Text buffered and never
        # emitted is silence, which is worse than the repeat it was avoiding.
        self._pending_repeat = ""
        # Survives across responses: repetition is a property of the CALL, not
        # of one reply, and this processor lives for the whole call.
        #
        # These two were lost in an edit and every turn after the first died
        # with "'ReplyFilter' object has no attribute '_said'" -- run 213 shows
        # fourteen pipeline errors and a caller asking "హలో, ఎందుకండీ ఇంత స్టాప్
        # అయిపోతుంది". The unit tests missed it because they build this object
        # with __new__ and set the fields by hand, so __init__ never ran.
        self._said: list[str] = []
        # Is the caller speaking RIGHT NOW?
        #
        # Defect 4 of the four that got semantic turn completion reverted. With
        # deferral the turn is never "stopped" while the LLM is being polled, so
        # a stale marker arriving after the caller has resumed leaves the turn
        # open -- correctly -- but nothing cancels the generation that came with
        # it. No interruption frame is raised, because from the pipeline's point
        # of view the bot never took the floor. So the reply is pushed to TTS on
        # top of a caller who is mid-sentence: the exact failure the feature was
        # added to prevent.
        #
        # Guarding it here rather than in the controller because this is the
        # last processor before TTS, and the rule is worth having whatever
        # produced the text: never speak over someone who is speaking.
        self._user_speaking = False
        # Set when a reply began on top of the caller. See LLMFullResponseStart.
        self._stale = False


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

        # Never speak over someone who is speaking.
        #
        # `_stale` means this whole generation began while the caller was still
        # mid-sentence, so it answers something he has since added to. Under
        # semantic turn completion the turn is deliberately NOT stopped while
        # the LLM is polled, so no interruption frame is raised and nothing else
        # in the pipeline will stop this text -- from its point of view the bot
        # never took the floor. Silently dropped rather than substituted: there
        # is nothing to apologise for, he simply has not finished, and the next
        # generation will answer the whole of what he said.
        if self._stale:
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
        # Before the repeat test can run at all, there has to be enough of the
        # reply to judge -- and for most of this agent's life there never was.
        #
        # Run 863, measured end to end:
        #
        #   BOT : సైట్ సర్వే కోసం మీరు సిద్ధంగా ఉన్నారా?
        #   USER: ఉన్నాము ఉన్నాము.                        (we are ready)
        #   BOT : సరే, సైట్ సర్వే కోసం మీరు సిద్ధంగా ఉన్నారా?
        #
        # `_is_repeat` gets that pair right -- handed the whole second reply it
        # returns True. It was never handed it. Two constants were never
        # reconciled: `ReplySanitizer` releases after **24** characters, so the
        # first `candidate` is ~24 characters INCLUDING the leading "సరే, ",
        # while `_is_repeat` needs `_REPEAT_PREFIX` (**25**) characters of
        # substance AFTER `_strip_ack` removes that acknowledgement. The first
        # chunk could never clear the bar, and once it was emitted `_spoken`
        # was non-empty and the check never ran again.
        #
        # The state block ASKS every reply to open with an acknowledgement, so
        # this was not an edge case: the guard was structurally unable to fire,
        # on every turn, of every call.
        #
        # It is fixed by waiting for one more chunk -- but only when a repeat is
        # actually SUSPECTED. Waiting unconditionally would put a chunk of
        # latency on every turn of every call to catch something rare, and
        # waiting after audio is out is forbidden outright: that is the
        # truncation bug, where callers heard "అర్థమైంది బిల్లు?". So the delay
        # is paid only when what we have so far already looks like the opening
        # of something we have said before, and never once anything has been
        # spoken. A false suspicion costs one chunk of latency; it can never
        # cause a wrong substitution, because the real `_is_repeat` still has to
        # agree before anything is replaced.
        if not self._spoken:
            pending = self._pending_repeat + candidate
            head = _normalise(_strip_ack(pending))
            if len(head) < _REPEAT_PREFIX and self._looks_like_repeat(head):
                self._pending_repeat = pending
                return ""
            self._pending_repeat = ""
            candidate = pending

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

        # A field already written down must never be asked again.
        #
        # Run 891: property type answered on turn 3, the bill confirmed back to
        # him on turn 4, and then property, bill, property asked across turns
        # 5, 6 and 7. Run 890, three turns after answering: "చెప్పాను కదా
        # కమర్షియల్ అని ఫస్ట్ లోనే చెప్పాను కదా" -- I told you commercial, I
        # said it right at the start.
        #
        # Neither existing guard covers this. `MAX_ASKS_PER_FIELD` counts asks,
        # and two is a legal budget -- for a field we have NOT got; it has
        # nothing to say about one already in `known`. `_is_repeat` compares
        # WORDING, and these re-asks are not always worded alike. Same lesson as
        # the ask budget: a guard on wording cannot catch a repeat of subject.
        #
        # Subject is read with `field_asked_in`, the matcher the ask budget
        # already uses, so there is nothing new to keep in step. Guarded by
        # `not self._spoken` like every other substitution here: once audio is
        # out, cutting the sentence in half is run 783's "అర్థమైంది బిల్లు?",
        # which is worse than the repeat.
        # getattr, not attribute access: several suites build the injector and
        # its state as doubles, and a double without this method must mean "do
        # not enforce" rather than an AttributeError on a live call. Run 213
        # shipped exactly that mistake from exactly this file.
        if not self._spoken and self._injector is not None:
            state = self._injector.state
            subject_of = getattr(state, "field_asked_in", None)
            asked_about = subject_of(candidate) if callable(subject_of) else ""
            known = getattr(state, "known", {})
            counts = getattr(state, "ask_counts", {}) or {}
            cap = getattr(state, "MAX_ASKS_PER_FIELD", 2)
            # TWO states end a question's life, and only one of them was being
            # enforced. Run 898 asked about the roof four times:
            #
            #   36:19  BOT   మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?
            #   36:32  USER  మాకు పెద్ద ఫ్యాక్టరీ ఉందండి ప్రస్తుతానికి
            #   36:39  BOT   మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?
            #   36:42  USER  మాకు ఫ్యాక్టరీ ఉంది
            #   36:44  BOT   మీకు రూఫ్ లేదా టెర్రస్ ఉందా?
            #
            # The budget was charged correctly and the field left STILL_NEED
            # after two. But it was never `known` -- he answered indirectly, by
            # naming a factory, and never said yes or no -- so the guard below
            # did not apply and nothing else stopped the model.
            #
            # Out of budget is not the same state as answered. A field whose
            # budget is spent has had every question it is ever going to get.
            spent = bool(asked_about) and counts.get(asked_about, 0) >= cap
            if asked_about and (asked_about in known or spent):
                self._blocked = True
                why = ("already known "
                       f"({known.get(asked_about)!r})" if asked_about in known
                       else f"out of budget ({counts.get(asked_about)} asks)")
                logger.warning(
                    f"[answered] {asked_about} is {why}; not asking it again: "
                    f"{candidate[:60]!r}")
                state.misheard_last_turn = True
                return guardrails.REPAIR_LINE

            # CHARGED HERE, where the question becomes real to the caller,
            # rather than only at `LLMFullResponseEndFrame`.
            #
            # The budget was failing on one or two fields every call -- run 897
            # bill 4 / location 3, run 898 roof 4 / survey 3, run 899 property 3
            # / location 3 -- and the calls with the most overruns were the
            # calls with the most double replies. That is the coupling: a reply
            # that is overtaken never reaches the End frame, so its question is
            # never charged, and an uncharged ask is one the budget cannot see.
            # `last_asked` is set there too, so `answered_pending` loses its
            # footing on exactly those turns.
            #
            # The trade is named rather than hidden. A question cut off
            # mid-word now counts, which run 783 argued against on the grounds
            # that the caller never heard the whole thing. Being asked the same
            # question four times is the complaint actually on the table, and
            # two asks is a generous budget. `charged_this_turn` keeps the two
            # charging points from counting one question twice.
            charge = getattr(state, "commit_ask", None)
            if asked_about and callable(charge):
                charge(candidate)
                state.ask_charged_on_air = True
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

    def _previous_replies(self) -> list[str]:
        """Everything this call has already said, from both stores.

        `self._said` lives on this object and survives the whole phone call;
        `state.asked` is persisted and survives the pipeline rebuild that text
        chat does per message. Factored out so `_is_repeat` and
        `_looks_like_repeat` can never drift apart about what "previous" means.
        """
        previous = list(self._said)
        state = getattr(self._injector, "state", None)
        if state is not None:
            previous.extend(getattr(state, "asked", []) or [])
        return previous

    def _looks_like_repeat(self, head: str) -> bool:
        """Is this partial reply the opening of something already said?

        Used ONLY to decide whether to wait for one more chunk before judging.
        It deliberately drops the `_REPEAT_PREFIX` length floor that
        `_is_repeat` enforces, because the whole point is to answer the
        question "might this become a repeat once there is enough of it".

        That makes it far looser than `_is_repeat`, and that is safe precisely
        because it decides nothing: a false positive costs one chunk of
        latency, and `_is_repeat` still has to agree before a word is replaced.

        The floor of 6 stops a bare "సరే" or a two-character fragment matching
        every previous reply and stalling the opening of every turn.
        """
        if len(head) < 6:
            return False
        for prev in self._previous_replies():
            other = _normalise(_strip_ack(prev))
            if len(other) < len(head):
                continue
            if SequenceMatcher(None, head, other[: len(head)]).ratio() >= (
                    _REPEAT_SIMILARITY):
                return True
        return False

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
        for prev in self._previous_replies():
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
        """`MODE: END` is how the agent hangs up -- if the call has earned it.

        Run 880. Six fields unasked, nothing refused, and the caller had just
        said he WAS interested:

            USER: we ARE interested in solar, but right now I don't know much
                  about it. Hello?
            BOT : no problem, call us whenever you like. Thank you.
                                                    *** hung up at 32s ***

        That is a man asking to be told about solar, which is the easiest lead
        on the list. Nothing deterministic did it -- every triage pattern was
        run against that exact sentence afterwards and not one matches. The
        model emitted `MODE: END` and this method set `must_end`, because
        nothing ever asked whether ending was earned. The same close appears in
        runs 866 and 876, so it is not new; it had never been looked at.

        Hanging up is a business action and the model does not own those: it
        owns language, the application owns truth. A wrong word costs a
        sentence; a wrong hangup costs the lead and the call is not coming back.

        So END is a REQUEST now. It is granted when the call has a reason to be
        over and ignored otherwise. The escape hatch is already there and does
        not need adding: say goodbye twice and `render()` sets `must_end`
        itself (run 803's rule), so an agent that really is finished still gets
        out on the next turn.
        """
        if not (self._injector and self._sanitizer.mode == "END"):
            return
        state = self._injector.state
        if _end_is_earned(state):
            state.must_end = True
            return
        logger.warning(
            "[end-call] MODE: END refused -- nothing refused, nothing "
            f"disqualified, and {len(state.still_need)} field(s) still unasked. "
            "Run 880 hung up on an interested caller here.")

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # A reply is abandoned only on EVIDENCE that he really spoke.
        #
        # This was keyed on `UserStartedSpeakingFrame` when it shipped, and bare
        # VAD fires without the caller saying anything new. Run 881 shows the
        # worst case in the transcript itself: the agent's own greeting came
        # back as USER text, word for word, off a speakerphone. `echoes_agent`
        # drops echoed TEXT before triage, but nothing drops the VAD event that
        # arrives with it -- so the agent's own voice silenced its own reply.
        #
        # And a dropped reply is never retried. Nothing regenerates, because a
        # new reply needs a new TURN and bare VAD does not complete one, so the
        # caller sits in silence until he speaks again. That is the "హలో" in
        # runs 881 and 882, and it is a worse failure than the overlap this
        # rule exists to prevent.
        #
        # A real transcription is self-healing: the same words that abandon this
        # reply are the ones that start the next turn, so the replacement is
        # already on its way before the caller notices.
        if isinstance(frame, TranscriptionFrame):
            heard = (frame.text or "").strip()
            state = self._injector.state if self._injector else None
            if (heard and self._generating and not self._spoken.strip()
                    and not (state is not None
                             and echoes_agent(heard, state.asked))):
                self._stale = True
                logger.warning(
                    "[turn] the caller spoke while this reply was being built; "
                    "dropping what had not been spoken yet")

        if isinstance(frame, UserStartedSpeakingFrame):
            self._user_speaking = True
            # He has taken the floor while a reply is still being generated.
            #
            # `_stale` was a SNAPSHOT, taken once at LLMFullResponseStartFrame,
            # of whether he happened to be speaking at that instant. A
            # generation that starts in silence and is overtaken while it
            # streams was therefore never marked, and every later chunk passed
            # the gate. Run 865 is what that sounds like -- two replies in one
            # breath, the second arriving on top of the first:
            #
            #     BOT : మీరు ఇంకా ఇక్కడ
            #           సరే, మీది సొంత ఇల్లా, అపార్ట్‌మెంటా...
            #
            # Deliberately NOT decided here any more -- see the
            # TranscriptionFrame branch above. Bare VAD fires on echo and on
            # noise, and a reply dropped on that evidence is never retried,
            # which is dead air (runs 881, 882).
            #
            # The rule it enforces is unchanged and still matters: only while
            # NOTHING has been spoken yet. Once audio is out the reply must
            # finish, because cutting a sentence in half is run 783's
            # truncation ("అర్థమైంది బిల్లు?") -- a worse thing to do to a
            # caller than answering him a beat late. That window exists at all
            # because the sanitizer holds back 24 characters.
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user_speaking = False

        if isinstance(frame, LLMFullResponseStartFrame):
            # One sanitizer per response; it carries per-reply truncation state.
            if self._spoken.strip():
                self._said.append(self._spoken)
                # The ask is NOT charged here. It is charged once, when the
                # reply ENDS -- see the LLMFullResponseEndFrame branch.
                #
                # Charging in both places cost a real call. Run 783:
                #
                #   BOT : సరే, మీరు ఏ ఏరియా లేదా సిటీలో    <- barge-in, cut off
                #   BOT : సరే, మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?
                #
                # The caller was never asked where he lives again and the saved
                # lead has `location: null`. The end frame charged it, the
                # barge-in re-rendered the state block and put the same field
                # back into `pending_ask`, and this branch charged it a second
                # time. Two charges is the entire two-ask budget, so the field
                # left `still_need` after one interrupted question.
                #
                # A reply cut off before it ends is now not charged at all,
                # which is the right answer on its own terms: the caller never
                # heard the whole question.
            # Rebuilt per reply on purpose: the caller's name is
            # usually learned halfway through the call, so it is
            # read fresh rather than captured at construction.
            self._sanitizer = ReplySanitizer(self._caller_names())
            self._spoken = ""
            self._pending_repeat = ""
            # A generation that BEGINS while the caller is mid-sentence is
            # stale: whatever it is answering, he has since said more. Marked
            # here for the whole reply rather than tested per fragment -- a
            # reply that starts on top of him and continues after he pauses is
            # still a reply to the wrong thing, and half of it is worse than
            # none.
            if self._in_flight is not None:
                self._in_flight.begin()
            self._stale = self._user_speaking
            if self._stale:
                logger.warning("[turn] a reply started while the caller was "
                               "still speaking; not speaking it")
            # A generation is in flight from here until the End frame. Read by
            # the UserStartedSpeakingFrame branch, which can only judge a reply
            # stale mid-flight if it knows one is being generated.
            self._generating = True
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
            if self._in_flight is not None and speakable.strip():
                self._in_flight.note_spoken()

        if isinstance(frame, LLMFullResponseEndFrame):
            if self._in_flight is not None:
                self._in_flight.done()
            self._generating = False
            # Held text is not exempt from the rules the stream obeys.
            #
            # The flush below pushes `_pending_repeat` straight to TTS -- it
            # never passed through `_gate`, which is the one place `_stale` is
            # consulted. So a reply that was correctly judged stale still spoke
            # whatever the repeat check happened to be holding, which on a
            # short reply is the whole of it. Dropped rather than substituted,
            # for the same reason `_gate` drops: he has not finished, and there
            # is nothing to apologise for.
            if self._stale:
                self._pending_repeat = ""
                self._sanitizer.finish()
                return
            # Flush anything held back by the repeat check FIRST.
            #
            # A reply can end while text is still buffered -- a short one is
            # entirely one chunk -- and text that is buffered and never emitted
            # is silence. That is a worse failure than the repeat it was
            # waiting to rule out, so the reply is now decidable and gets
            # decided: either it really was a repeat, and the repair line
            # replaces it, or it was not, and it is spoken exactly as written.
            if self._pending_repeat:
                held, self._pending_repeat = self._pending_repeat, ""
                if not self._spoken and self._is_repeat(held):
                    self._blocked = True
                    logger.warning(
                        "[repeat] already asked this; saying it could not hear "
                        f"instead: {held[:60]!r}")
                    if self._injector:
                        self._injector.state.misheard_last_turn = True
                    held = guardrails.REPAIR_LINE
                if held:
                    self._spoken += held
                    await self.push_frame(LLMTextFrame(held), direction)

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

                # An apology is not a question, and must not be billed as one.
                #
                # Run 783's trap, traced end to end: a barge-in cancels the
                # generation, so the half-spoken question never reaches this
                # branch and is correctly not charged -- but the fragment does
                # land in `_said`. The model re-asks, `_is_repeat` matches the
                # new full question against that fragment, and `_gate`
                # substitutes REPAIR_LINE. The repair line then completes
                # normally, reaches here, and charges the field for a question
                # the caller never heard. Two of those and the field is
                # abandoned: the agent spends the caller's question budget
                # apologising for its own interruption.
                is_repair = said == guardrails.REPAIR_LINE.strip()[:90]
                # Already charged as it went on the wire -- see `_gate`. Not
                # charged twice for one question.
                already = getattr(self._injector.state,
                                  "ask_charged_on_air", False)
                if not is_repair and not already:
                    # The sentence is handed over so the ask is charged to the
                    # field it ASKED ABOUT, not the one the state block
                    # nominated. Runs 872/879/885 diverged, and the budget was
                    # spent on the wrong field every time.
                    self._injector.state.commit_ask(said)

                # Run 803: the goodbye is an event, not a standing order. Until
                # something recorded that it had been delivered, `render()`
                # re-issued the identical closing instruction on every turn --
                # seven times, to a caller asking to be let go.
                self._injector.state.note_reply_delivered()
                # The reply is out, so whatever he says next is a NEW turn.
                # Without this, one question would withdraw the checklist for
                # the rest of the call.
                self._injector.state.end_user_turn()

                # One structured record per turn. Emitted HERE because this is
                # the point where the turn is finished and every counter has
                # settled -- the reply is spoken, the ask is charged, the
                # closing is noted. Logged as a single line so a whole call can
                # be read back afterwards without re-deriving state from a
                # transcript, which is how two wrong diagnoses were reached on
                # 11 Sep.
                try:
                    logger.info("[turn-log] " + json.dumps(
                        {**self._injector.state.turn_log(),
                         "said": said},
                        ensure_ascii=False, default=str))
                except Exception as exc:      # never let logging break a call
                    logger.warning(f"[turn-log] could not be written: {exc}")

        await self.push_frame(frame, direction)
