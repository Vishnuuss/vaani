"""Properties of a WHOLE call, not of one turn.

Every defect found on 23 Sep was found by a human reading a transcript days
after it shipped. There is a gate -- `tools/vaani_eval.py` -- but it drives the
DEPLOYED server over text chat, so it cannot catch anything before a deploy, and
by then the caller has already heard it.

The unit suites next to this file are all shaped the same way: build one
`ReplyFilter`, call `_gate` once, assert on the answer. That shape is right for
what it tests and structurally cannot see the four failures that actually shipped
yesterday, because every one of them is a property of a SEQUENCE:

    run 1023   the apology was fine ONCE. Four times in a row is what made him
               hang up, and no single-turn assertion can count to four.
    run 1027   asking the city is fine. Asking it again after storing it is not,
               and "again" is a fact about turn 7 relative to turn 2.
    run 1031   one "హలో" costs one question. Twelve of them cost the whole call:
               every field null, `end_call`, and a man who never heard a
               complete sentence.
    run 1012   one cancelled reply is correct behaviour. Three in a row is
               silence, and silence is what he actually experienced.

So this file drives the real objects -- `StateInjector`, `ReplyFilter`,
`CallState`, `triage`, `guardrails` -- across many turns, and asserts on what the
caller HEARD. A scenario is three lines:

    conv = Conversation()
    await conv.say("విజయవాడ.", learns={"location": "విజయవాడ"})
    await conv.say("లేదు.", draft=Q["location"])        # the run 1027 re-ask
    assert conv.heard[-1] != Q["location"]

Nothing here is mocked except the two seams the brain reads from and cannot
provide itself: the LLM context object, and Dograh's variable extractor. The
model's words are an INPUT, because that is what the brain's contract is -- it
judges text it did not write -- and `draft=None` makes the model obey the state
block instead, which closes the loop for the scenarios that are about the
checklist advancing.

Three of the properties below FAIL on today's code and are marked
`xfail(strict=True)` with the measurement that says so. They are not aspirations:
each one is a transcript from yesterday that the current guards do not prevent.
Strict, so that the day someone fixes one, this file fails loudly and the marker
has to come off rather than the finding being forgotten again.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import pytest
from loguru import logger

from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from api.services.vaani import client_reference, completeness, guardrails, triage
from api.services.vaani.brain_processor import ReplyFilter, StateInjector
from api.services.vaani.compiler import Brief
from api.services.vaani.reply_sanitizer import ReplySanitizer
from api.services.vaani.state import _is_question

# MB Solar's four qualifying questions, in the client's own words -- the same
# four the other suites in this directory use, so a failure here is comparable
# with a failure there.
Q = {
    "property_type": "మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?",
    "monthly_bill": "మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?",
    "location": "మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?",
    "roof_available": "మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?",
}

# The written re-ask wordings a client supplies alongside the question
# (`CallState.question_variants`). Used where a scenario needs a SECOND ask of
# the same field to be a legal, human-written sentence rather than an invention
# -- which is how the live agent re-asks, and the only way to spend a field's
# two-ask budget without the wording-repeat guard standing in for the test.
VARIANTS = {
    "location": [Q["location"], "మీ ఏరియా ఏది అండి, ఏ సిటీలో ఉన్నారు?"],
}

# The client's answer bank -- the half of Layer 3 that lives below `## Reference`
# and is consulted per turn rather than compiled into the prompt. Verbatim
# shapes from MB Solar's file; these are the three most-asked questions on a
# solar call.
SUBSIDY = ("PM Surya Ghar స్కీమ్ లో మొదటి two kW కి thirty thousand rupees, "
           "ఆ తర్వాత kW కి eighteen thousand rupees, మొత్తం seventy eight "
           "thousand rupees వరకు వస్తుంది అండి.")
WARRANTY = "ప్యానెల్స్ twenty five years కంటే ఎక్కువ వస్తాయి అండి."
# Real rows -- **heading**, TRIGGER, quoted answer -- because that is the only
# shape `client_reference.parse` serves. An earlier version of this constant was
# the two answers as bare text: enough to feed the number whitelist, never
# enough to be SERVED, so no scenario in this file ever ran a reference turn and
# the subsidy test could not pass whatever the code did.
ANSWER_BANK = (
    "## Reference\n\n"
    "**Is the subsidy real?**\n"
    "TRIGGER: సబ్సిడ|subsid|స్కీమ\n"
    f'"{SUBSIDY}"\n\n'
    "**Warranty?**\n"
    "TRIGGER: వారంట|warrant\n"
    f'"{WARRANTY}"\n'
)

# A compiled prompt that CONTAINS numbers. It has to: an empty whitelist means
# "no knowledge base was compiled" and `no_invented_quantity` is then not
# enforced at all, so a test that used a number-free prompt would be measuring
# the rule being switched off rather than the rule passing.
COMPILED_PROMPT = ("## OUTPUT CONTRACT\n- Never invent a number. Not one.\n"
                   "Ask about ఒక or రెండు things.\n")

CANNED = {
    guardrails.REPAIR_LINE.strip(),
    guardrails.SAFE_FALLBACK.strip(),
    guardrails.SAFE_CLOSE.strip(),
}

SENTENCE_END = "?।.!"


def sanitised(draft: str, names: tuple = ()) -> str:
    """What `ReplySanitizer` alone would release for this draft.

    The oracle below compares what the caller heard against this rather than
    against the raw draft, because the sanitizer legitimately rewrites -- it
    substitutes ~55 written-register nouns (`speech_register.spoken`), fixes
    `అండి`/`గారు` after a name, and truncates at the turn's first question mark.
    Comparing against the raw draft would call every one of those a fragment.
    """
    s = ReplySanitizer(names)
    return (s.feed(draft or "") + s.finish()).strip()


def is_whole_line(heard: str, draft: str, legal: set) -> bool:
    """Did the caller hear a COMPLETE line, or a piece of one?

    Run 783 is the failure this answers. The caller heard

        "అర్థమైంది బిల్లు?"

    -- an acknowledgement welded to the tail of a question -- because a guard
    substituted after audio was already out. A live reply came out as

        "సరే, రెండు thousand rupeeసార్, కరెక్ట్ ఫిగర్ ఇప్పుడే చెప్పలేను."

    -- the safe line grafted into the middle of a word. Both are worse than the
    thing they were replacing, and both are invisible to a per-turn assertion
    that only checks WHICH line was chosen.

    Four things are whole:
      * exactly what the sanitizer released for this draft
      * one of the canned lines, entire
      * one of the client's own configured questions, entire
      * the draft cut at its FIRST sentence end with a whole sentence dropped
        after it -- the deliberate one-question-per-turn rule, which is a
        truncation but never a fragment

    Anything else is a piece of a sentence and the caller should never hear it.
    """
    h = (heard or "").strip()
    if not h:
        # Silence is a real failure but a different one; `test_run_1012` owns it.
        return True
    if h in legal or h in CANNED:
        return True
    d = sanitised(draft)
    if h == d:
        return True
    if d.startswith(h):
        return h[-1] in SENTENCE_END and bool(d[len(h):].strip())
    return False


class _Context:
    """The LLM context object `StateInjector` rewrites each turn.

    Only two methods are touched (`messages`, `set_messages`), so this is the
    whole seam rather than a simplification of it.
    """

    def __init__(self) -> None:
        self.messages: list = []

    def set_messages(self, messages) -> None:
        self.messages = list(messages)


class _Engine:
    """Dograh's engine, which owns variable extraction.

    `StateInjector._learn_from_engine` reads `_gathered_context`
    ["extracted_variables"] and copies it into `known`. That is the ONLY path by
    which a non-money field becomes known on a live call, so a conversation test
    that did not provide it would run with `known` permanently empty -- which is
    precisely the state the agent was in before run 977, and would make every
    "a known field is never asked again" assertion pass for the wrong reason.
    """

    def __init__(self) -> None:
        self._gathered_context = {"extracted_variables": {}}


@dataclass
class Exchange:
    caller: str
    draft: str
    heard: str
    known: dict = field(default_factory=dict)
    cut_off: bool = False
    stale: bool = False


class Conversation:
    """One phone call, driven through the real brain, remembering what was heard."""

    def __init__(self, questions: dict | None = None, *, variants: dict | None = None,
                 known: dict | None = None, system_prompt: str = COMPILED_PROMPT,
                 reference_text: str = "") -> None:
        questions = dict(questions or Q)
        brief = Brief(business="MB Solar",
                      questions=[{"field": k, "ask": v}
                                 for k, v in questions.items()])
        self.context = _Context()
        self.engine = _Engine()
        self.injector = StateInjector(brief, self.context, system_prompt,
                                      engine=self.engine,
                                      reference_text=reference_text)
        # Exactly what `run_pipeline.build_vaani_brain` does on a live call.
        # Without it `render()` has no rows to serve, `reference_text_this_turn`
        # stays empty, and every reference turn is measured as an ordinary one.
        if reference_text:
            self.injector.state.reference = client_reference.parse(reference_text)
        if variants:
            self.injector.state.question_variants.update(variants)
        for name, value in (known or {}).items():
            self.injector.state.learn(name, value)
        self.filter = ReplyFilter(self.injector)
        self._out: list[str] = []

        async def _collect(frame, direction=FrameDirection.DOWNSTREAM):
            if isinstance(frame, LLMTextFrame):
                self._out.append(frame.text)

        self.filter.push_frame = _collect
        self.log: list[Exchange] = []
        self.hung_up_on_turn = -1

    # -- what the call knows ------------------------------------------------
    @property
    def state(self):
        return self.injector.state

    @property
    def heard(self) -> list[str]:
        return [x.heard.strip() for x in self.log]

    @property
    def legal_lines(self) -> set:
        """Every whole sentence the client wrote, in any of its wordings."""
        out = set()
        for name in self.state.questions:
            for wording in self.state.variants_for(name):
                out.add(wording.strip())
        return out

    def nominated_question(self) -> str:
        """The question the state block is telling the model to ask.

        Passing `draft=None` to `say` makes the model obey it, which is how the
        real agent behaves on the great majority of turns -- the block is the
        last thing in the context and the loudest. It is what lets a scenario
        about the CHECKLIST advancing be driven without scripting the answer
        the test is trying to measure.
        """
        st = self.state
        name = st.pending_ask or (st.still_need[0] if st.still_need else "")
        return st.questions.get(name) or ""

    def asked_about(self, line: str) -> str:
        """Which field this spoken sentence asks about, per the real matcher."""
        return self.state.field_asked_in(line or "")

    def times_asked_about(self, name: str) -> int:
        return sum(1 for line in self.heard if self.asked_about(line) == name)

    # -- driving the call ---------------------------------------------------
    async def say(self, caller: str, draft: str | None = None, *,
                  chunk: int | None = None, cut_off: bool = False,
                  interrupted_by: str = "", learns: dict | None = None) -> str:
        """One turn: he speaks, the model drafts, and the caller hears something.

        `learns`   lands values in Dograh's extractor BEFORE this turn's triage,
                   which is where the async extractor's verdict arrives -- one
                   turn after the words that produced it. Pass it on the turn
                   AFTER he answered, the way the real thing behaves.
        `chunk`    splits the draft into frames of this many characters. It
                   matters enormously and is not a detail: the repeat guard can
                   only judge text the sanitizer has released, and what it has
                   released depends entirely on how the LLM streamed.
        `cut_off`  no `LLMFullResponseEndFrame` -- a barge-in cancelled the
                   reply. The ask is deliberately not charged (run 783).
        `interrupted_by`
                   a final transcript arriving while the reply is being built.
                   That is the evidence `ReplyFilter` requires before abandoning
                   a reply, and run 1012 is three of them in a row.
        """
        assert self.hung_up_on_turn < 0, (
            f"the call hung up on turn {self.hung_up_on_turn} "
            f"(must_end was set); anything after that is not a real turn")
        if learns:
            self.engine._gathered_context["extracted_variables"].update(learns)
        if draft is None:
            draft = self.nominated_question()

        self.injector.note_user_text(caller)
        self._out.clear()
        await self._send(LLMFullResponseStartFrame())
        if interrupted_by:
            await self._send(TranscriptionFrame(interrupted_by, "caller", "t"))
        for piece in ([draft] if chunk is None else
                      [draft[i:i + chunk] for i in range(0, len(draft), chunk)]):
            await self._send(LLMTextFrame(piece))
        if not cut_off:
            await self._send(LLMFullResponseEndFrame())

        heard = "".join(self._out)
        self.log.append(Exchange(caller=caller, draft=draft, heard=heard,
                                 known=dict(self.state.known), cut_off=cut_off,
                                 stale=bool(interrupted_by)))
        if self.state.must_end and self.hung_up_on_turn < 0:
            self.hung_up_on_turn = len(self.log) - 1
        return heard

    async def _send(self, frame) -> None:
        await self.filter.process_frame(frame, FrameDirection.DOWNSTREAM)


# --- invariants, applied to any conversation ---------------------------------
def no_line_heard_twice_in_a_row(conv: Conversation) -> list[str]:
    """Run 1023: four identical apologies, and he hung up."""
    said = [h for h in conv.heard if h]
    return [a for a, b in zip(said, said[1:]) if a == b]


def no_known_field_asked_again(conv: Conversation) -> list[tuple[str, str]]:
    """Run 1027: the city was stored on turn 2 and asked again on turn 7."""
    bad = []
    for x in conv.log:
        name = conv.asked_about(x.heard)
        if name and name in x.known:
            bad.append((name, x.heard[:40]))
    return bad


def no_fragments(conv: Conversation) -> list[tuple[str, str]]:
    """Run 783: "అర్థమైంది బిల్లు?" -- half a sentence is worse than a repeat."""
    legal = conv.legal_lines
    return [(x.heard, x.draft) for x in conv.log
            # A barge-in cuts the audio itself; that is not the gate splicing.
            if not x.cut_off and not is_whole_line(x.heard, x.draft, legal)]


def distinct_questions_asked(conv: Conversation) -> set:
    """Which fields the caller was actually asked about, across the whole call.

    "Distinct questions must not exceed the number of fields" is the weakest
    property in this file and is kept only as a cheap sanity check on the
    accounting: `field_asked_in` maps into the checklist by construction, so the
    count can only exceed `len(fields)` if the agent asked about something that
    is not on it at all. Run 1031 satisfies it -- four fields, four questions,
    every value null -- which is why the tests that matter count asks PER FIELD
    (`times_asked_about`) and check what the state block nominates next.
    """
    return {conv.asked_about(h) for h in conv.heard} - {""}


# --- the premises these fixtures rest on -------------------------------------
def test_the_premises_of_every_fixture_below_hold():
    """Assert what the tests assume, so none of them can measure nothing.

    A test written yesterday assumed "మరి నేను" was grammatically unfinished;
    `completeness.sounds_unfinished` says it is not, so the scenario built on it
    was exercising a path that never ran. Every assumption this file makes about
    the real matchers is stated here instead of being trusted.
    """
    # Run 1012's utterance is NOT caught by the text-side completeness rules.
    # It is two words and a full stop, and it reads finished. The refund in that
    # scenario therefore cannot come from `sounds_unfinished`, which is why the
    # test below asserts on the cancelled reply and not on a refund.
    assert completeness.sounds_unfinished("మాది.") is False
    assert completeness.sounds_unfinished("మాది") is False

    # Run 1031's utterance IS a channel failure, and is NOT a question -- the
    # second half matters, because `note_answer_to_last_ask` only declines to
    # count an utterance as an answer when it is a question or pure hesitation.
    assert triage.SAY_IT_AGAIN.search("హలో")
    assert _is_question("హలో") is False
    assert "హలో" not in completeness.HESITATIONS

    # The field matcher really does recognise every wording used below. If it
    # returned "" the whole-call accounting would silently measure nothing.
    conv = Conversation(variants=VARIANTS)
    for name, line in Q.items():
        assert conv.asked_about(line) == name, f"{name} unmatched: {line!r}"
    assert conv.asked_about(VARIANTS["location"][1]) == "location"

    # And the fragment oracle really does reject run 783's fragment. A checker
    # that cannot fail is the thing this file exists to avoid.
    legal = conv.legal_lines
    assert not is_whole_line("అర్థమైంది బిల్లు?",
                             "అర్థమైంది, " + Q["monthly_bill"], legal)
    assert not is_whole_line("సరే, రెండు thousand rupee"
                             + guardrails.SAFE_FALLBACK,
                             "సరే, రెండు thousand rupees అవుతుంది అండి.", legal)
    assert is_whole_line(Q["location"], "సరే, " + Q["location"], legal)
    assert is_whole_line(guardrails.REPAIR_LINE, Q["location"], legal)


async def test_the_harness_hears_what_the_caller_hears():
    """The control. Without it every assertion below could be measuring silence."""
    conv = Conversation()
    heard = await conv.say("చెప్పండి అండి", "నమస్కారం అండి, " + Q["location"])
    assert Q["location"] in heard
    assert conv.heard == [heard.strip()]
    assert conv.state.ask_counts.get("location") == 1, (
        "a completed question must be charged, or the budget tests mean nothing")
    assert not no_fragments(conv)


# --- run 1027: the city it had already written down ---------------------------
async def test_run_1027_a_stored_field_is_never_asked_again():
    """
        BOT  మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?   asks the city
        USER విజయవాడ.                                stored
        ...
        BOT  మంచిది, మీరు ఏ ఏరియా లేదా సిటీలో...?    the city AGAIN

    Driven end to end: the extractor lands `location` a turn late, exactly as it
    does live, and the model then re-asks it anyway.
    """
    conv = Conversation()
    await conv.say("చెప్పండి", "నమస్కారం అండి, " + Q["location"])
    await conv.say("విజయవాడ.", "సరే అండి, " + Q["roof_available"],
                   learns={"location": "విజయవాడ"})
    heard = await conv.say("లేదు.", "మంచిది, " + Q["location"])

    assert Q["location"] not in heard, "it asked for the city it already had"
    assert heard.strip() == Q["property_type"], (
        "a blocked re-ask becomes the next field we actually need; "
        f"got {heard!r}")
    assert not no_known_field_asked_again(conv)
    assert not no_fragments(conv)


async def test_run_1027_the_bill_too_and_the_call_still_moves():
    """The same failure on a second field, and the call must still progress."""
    conv = Conversation()
    await conv.say("చెప్పండి", "నమస్కారం అండి, " + Q["monthly_bill"])
    await conv.say("యాభై వేలు.", "సరే అండి, " + Q["location"],
                   learns={"monthly_bill": "50000", "location": "విజయవాడ"})
    await conv.say("విజయవాడ అండి.", "మంచిది, " + Q["monthly_bill"])
    await conv.say("చెప్పాను కదా.", "సరే, " + Q["location"])

    assert not no_known_field_asked_again(conv)
    assert distinct_questions_asked(conv) <= set(Q), (
        "the caller was asked about something that is not on the checklist")
    assert len(distinct_questions_asked(conv)) <= len(Q)


# --- run 1023: four apologies to a caller it had heard perfectly --------------
async def test_run_1023_the_apology_is_never_heard_twice_in_a_row():
    """
        USER కొంత రూఫ్ ఉంది.                      roof answered, clearly
        BOT  క్షమించండి, సరిగ్గా వినిపించలేదు...   x4, and he hung up

    `roof_available` was already known, so every blocked re-ask substituted the
    same apology. Four identical lines is the failure; one is the feature.
    """
    conv = Conversation(known={"property_type": "own", "roof_available": "true"})
    for caller in ("కొంత రూఫ్ ఉంది.", "ఆ, ఉందండి, ఉంది.", "కొంత రూఫ్ ఉంది.",
                   "ఏం వినబడలేదండి మీకు, కొంత రీఫ్ ఉందని చెప్తున్నా నేను"):
        if conv.hung_up_on_turn >= 0:
            break
        await conv.say(caller, "సరే, " + Q["roof_available"])

    assert not no_line_heard_twice_in_a_row(conv), (
        f"the caller heard the same line twice running: {conv.heard}")
    apologies = conv.heard.count(guardrails.REPAIR_LINE.strip())
    assert apologies <= 1, (
        f"the apology is a one-shot; heard {apologies} times: {conv.heard}")
    assert conv.times_asked_about("roof_available") == 0, (
        "the roof was already known and must never be asked again")
    assert not no_fragments(conv)


async def test_run_1023_the_apology_is_a_last_resort_not_a_first_one():
    """With somewhere to go, the caller hears the next question, not sorry.

    This is the half that run 1027 proved was missing: "we must not ask this" is
    a question about what to ask NEXT, not about what to SAY.
    """
    conv = Conversation(known={"roof_available": "true"})
    heard = await conv.say("కొంత రూఫ్ ఉంది.", "సరే, " + Q["roof_available"])
    assert heard.strip() != guardrails.REPAIR_LINE.strip()
    assert heard.strip() in conv.legal_lines


async def test_the_reason_a_reply_was_replaced_is_logged_through_loguru():
    """A blocked re-ask has to be readable in the call log afterwards.

    Asserted through a loguru sink, NOT pytest's `caplog`. A test written
    yesterday used `caplog`, which sees nothing this codebase emits -- every
    module here logs through `loguru.logger` -- so it measured an empty string
    and passed. The sink below is the only way to see these lines, and the first
    assertion checks the sink itself caught something before the second one
    looks for a particular line in it.
    """
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        conv = Conversation(known={"location": "విజయవాడ"})
        await conv.say("విజయవాడ అండి.", "మంచిది, " + Q["location"])
    finally:
        logger.remove(sink_id)

    assert lines, "the loguru sink captured nothing; this test was measuring air"
    joined = "\n".join(lines)
    assert "[answered]" in joined, (
        "the substitution must say why it happened, or the next transcript read "
        f"is another day of guessing. Captured:\n{joined[:600]}")
    assert "location" in joined


# --- run 1031: every "హలో" cost him a question he never heard -----------------
async def test_run_1031_a_channel_failure_brings_the_question_back():
    """
        BOT  మీది సొంత ఇల్లా,          cut off mid-question
        USER హలో.
        BOT  మీ కరెంట్ బిల్లు నెలకి     jumped to the BILL

    Measured on `location`, which is THIRD on the checklist, and after both of
    its asks have been spent -- so if the refund fails the field is abandoned
    and the state block names a different subject. That is the whole failure,
    and an assertion on a field that happened to be first in the list would pass
    without it.
    """
    conv = Conversation(variants=VARIANTS,
                        known={"property_type": "own", "monthly_bill": "50000"})
    await conv.say("చెప్పండి", "సరే అండి, " + VARIANTS["location"][0])
    await conv.say("ఏంటి అండి", VARIANTS["location"][1])
    assert conv.state.ask_counts.get("location") == 2, (
        "both asks must really have been spent, or there is nothing to refund")
    assert "location" not in conv.state.still_need, (
        "the premise: out of budget, the field has left the checklist")

    conv.injector.note_user_text("హలో")
    block = conv.state.render()

    assert "location" in conv.state.still_need, (
        "run 1031 lost the field entirely and saved a null")
    assert conv.state.still_need[0] == "location"
    assert "LOCATION" in block, (
        "the state block must still name the subject he did not hear")
    assert "ROOF" not in block, "it moved on to a question he had even less chance of hearing"
    assert "ask the SAME question again" in block


async def test_run_1031_an_ordinary_word_does_not_bring_it_back():
    """The counterfactual, which is also the OLD behaviour.

    Before "హలో" was recognised, every utterance took this path: the ask stayed
    spent, the field stayed abandoned, and the block named the next subject
    down. If the two branches ever converge this file goes red -- and without
    this half, the test above would pass on code where nothing is refunded at
    all, since `still_need` is simply the checklist in order.
    """
    conv = Conversation(variants=VARIANTS,
                        known={"property_type": "own", "monthly_bill": "50000"})
    await conv.say("చెప్పండి", "సరే అండి, " + VARIANTS["location"][0])
    await conv.say("ఏంటి అండి", VARIANTS["location"][1])

    conv.injector.note_user_text("ఆ సరే")
    block = conv.state.render()

    assert "location" not in conv.state.still_need
    assert "LOCATION" not in block
    assert "ROOF" in block, (
        "this is the run 1031 skip; it is asserted here so the test above "
        "cannot pass vacuously")


async def test_a_question_cut_off_by_a_barge_in_is_not_charged():
    """Run 783: an interrupted question spent its whole budget and was lost.

    The first line of run 1031 was cut off mid-question. If that ask is charged,
    the refund the next "హలో" earns is already too late.
    """
    conv = Conversation()
    await conv.say("హలో చెప్పండి", "సరే అండి, మీది సొంత ఇల్లా, అపార్ట్‌",
                   cut_off=True)
    assert conv.state.ask_counts.get("property_type", 0) == 0, (
        "he never heard the whole question, so it must not be charged")
    assert "property_type" in conv.state.still_need

    heard = await conv.say("హలో", Q["property_type"])
    assert heard.strip() == Q["property_type"], (
        "the question he was cut off on must actually reach him")


@pytest.mark.xfail(strict=True, reason=(
    "DESIGN TRADE-OFF, not a defect -- read before 'fixing'. The door this "
    "marker was first written for is CLOSED: as of 24 Sep 'హలో' no longer "
    "counts as an answer (`state._asks_to_hear_it_again`), and the realistic "
    "run 1031 shape -- one హలో per question, then the answer -- keeps every "
    "budget; see test_the_real_run_1031_shape_keeps_every_budget. What still "
    "fails here is the pure dead-line case, ten హలోs and nothing else, where "
    "MAX_REFUNDS_PER_FIELD = 1 lets `ask_counts` reach the cap and retire the "
    "field. That cap IS run 870's fix: uncapped refunds asked one question "
    "forever until the caller hung up. Making this pass by lifting the cap "
    "reopens run 870. On a dead line both outcomes store the same nulls; the "
    "real question is whether the agent should keep asking or close, and that "
    "is a product decision, not a regex."))
async def test_run_1031_a_call_of_nothing_but_hello_loses_no_fields():
    """Twelve turns, `end_call`, every extracted field null.

    The model obeys the state block here (`draft=None`), so this measures the
    CHECKLIST, not a scripted reply: what the agent chooses to ask next, turn
    after turn, when the caller has told it nine times that he cannot hear.

    A caller who has answered nothing must still have everything to answer.
    """
    conv = Conversation()
    for _ in range(10):
        if conv.hung_up_on_turn >= 0:
            break
        await conv.say("హలో")

    assert conv.state.known == {}, "he answered nothing; nothing can be known"
    assert conv.state.answer_counts == {}, (
        f"'హలో' was counted as an answer: {conv.state.answer_counts}")
    assert set(conv.state.still_need) == set(Q), (
        "every field is still unanswered, so every field is still needed; "
        f"still_need={conv.state.still_need}")


# --- run 1012: his own continuation cancelled the reply three times -----------
async def test_run_1012_a_reply_cancelled_by_his_continuation_costs_him_nothing():
    """
        USER "మాది."            ... and he draws breath
        BOT  (reply built, then cancelled by what he said next)   x3

    He heard nothing at all. The three cancellations are correct in isolation --
    never speak over someone who is speaking -- and the property that matters is
    what they add up to: no ask spent, no field lost, and the question still
    reaching him when he finally stops.
    """
    conv = Conversation()
    for _ in range(3):
        heard = await conv.say("మాది.", "సరే, " + Q["property_type"],
                               interrupted_by="మాది ఇల్లు")
        assert heard == "", f"it spoke over him: {heard!r}"

    assert conv.state.ask_counts.get("property_type", 0) == 0, (
        "a reply he never heard must not spend the question")
    assert "property_type" in conv.state.still_need

    heard = await conv.say("మాది సొంత ఇల్లు అండి.", "సరే, " + Q["property_type"])
    assert heard.strip().endswith(Q["property_type"]), (
        f"after three cancellations he must hear the whole question; got {heard!r}")
    assert conv.state.ask_counts.get("property_type") == 1, (
        "charged exactly once, for the one reply he actually heard")
    assert not no_fragments(conv)


# --- the repeat guard, over a whole call and at real streaming granularity ----
async def test_an_identical_re_ask_is_caught_when_the_reply_arrives_whole():
    """The guard does work -- on one frame. This is the control for the next test.

    `_is_repeat` needs `_REPEAT_PREFIX` (25) characters of substance after the
    leading acknowledgement is stripped, and it may only judge while nothing has
    been spoken. Both hold when the whole reply lands in a single frame.
    """
    conv = Conversation()
    await conv.say("చెప్పండి", "సరే అండి, " + Q["location"])
    heard = await conv.say("ఏంటి అండి", "అర్థమైంది సార్, " + Q["location"])
    assert heard.strip() == guardrails.REPAIR_LINE.strip(), (
        f"an identical question was said twice: {heard!r}")


async def test_an_identical_re_ask_is_caught_at_real_streaming_granularity():
    """
    Was a strict xfail until 24 Sep: the guard only fired when the LLM
    streamed in chunks of 12+ characters, because HOLDBACK releases
    `buffer[:-24]` as a 1-4 character sliver, `_looks_like_repeat` refused
    anything under 6, and the check never ran again once anything was
    spoken. Closed by never deciding on a sliver -- the same change that
    stopped run 1044 speaking its booking line three times. It went
    XPASS(strict) the moment that landed.
A real LLM streams a few characters at a time. Run 1027 and run 721 are
    both a question the caller heard twice, word for word, and the guard that
    exists to stop it cannot see a reply delivered that way."""
    conv = Conversation()
    await conv.say("చెప్పండి", "సరే అండి, " + Q["location"], chunk=4)
    heard = await conv.say("ఏంటి అండి", "సరే అండి, " + Q["location"], chunk=4)
    assert heard.strip() == guardrails.REPAIR_LINE.strip(), (
        f"an identical question was said twice: {heard!r}")


async def test_a_stubborn_model_cannot_ask_the_same_thing_for_ever():
    """At most two asks and one refund, so the caller hears a question three
    times at the very outside. Run 218 hung up after four.

    Was a strict xfail until 24 Sep: ten turns, the IDENTICAL sentence heard
    nine times, eight in a row. Three things were missing and all three were
    needed, found by tracing each turn rather than guessing:

      1. the repeat branch had no `_next_needed_question()` fallback, so a spent
         repair fell through to the repeat itself;
      2. with the checklist EXHAUSTED (`still_need == []`) and `must_close`
         False, there was nothing to move on to -- `must_close` does not treat
         an empty checklist as a finished call;
      3. returning SAFE_CLOSE did not set `must_end`, so the line stayed open and
         the closing line was heard seven turns running.

    Now: ask, apologise once, move on, close, hang up. It went XPASS(strict) the
    moment the third landed, which is the only reason the marker is gone.
    """
    conv = Conversation()
    for _ in range(10):
        if conv.hung_up_on_turn >= 0:
            break
        await conv.say("ఏంటో తెలియదు అండి", "సరే, " + Q["monthly_bill"])

    limit = conv.state.MAX_ASKS_PER_FIELD + triage.MAX_REFUNDS_PER_FIELD
    worst = Counter(conv.asked_about(h) for h in conv.heard)
    worst.pop("", None)
    assert not no_line_heard_twice_in_a_row(conv), (
        f"consecutive identical lines: {no_line_heard_twice_in_a_row(conv)[:3]}")
    assert all(n <= limit for n in worst.values()), (
        f"asked more than {limit} times: {dict(worst)}")


# --- the client's own figures -------------------------------------------------
async def test_the_answer_bank_is_spoken_when_the_client_wrote_it():
    """Layer 3 was split, and `known_numbers` was left reading the compiled half.

    So the client's own figures became "invented quantities" and the reply was
    replaced by SAFE_FALLBACK -- on the three most-asked questions of a solar
    call. Measured here end to end, with and without the reference half, so the
    fix is pinned by the difference rather than by an assertion that would pass
    either way.
    """
    with_bank = Conversation(reference_text=ANSWER_BANK)
    heard = await with_bank.say("వారంటీ ఎంత అండి?", WARRANTY)
    assert heard.strip() == WARRANTY.strip(), (
        f"the client wrote this sentence; the caller must hear it. Got {heard!r}")

    without = Conversation(reference_text="")
    blocked = await without.say("వారంటీ ఎంత అండి?", WARRANTY)
    assert blocked.strip() == guardrails.SAFE_FALLBACK.strip(), (
        "with only the compiled half whitelisted the same sentence is replaced "
        f"-- this is the defect, and it must stay demonstrable. Got {blocked!r}")


async def test_an_actually_invented_figure_is_still_replaced():
    """The guard must keep its teeth, or the test above is an invitation."""
    conv = Conversation(reference_text=ANSWER_BANK)
    heard = await conv.say("నెలకి ఎంత ఆదా?",
                           "మీకు నెలకి ninety nine thousand rupees ఆదా అవుతుంది అండి.")
    assert heard.strip() == guardrails.SAFE_FALLBACK.strip(), (
        "ninety nine thousand appears in neither half of the client's file")


async def test_the_subsidy_answer_reaches_the_caller():
    """The figures are a government scheme's, written in the client's own file,
    and the reason the answer bank exists at all.

    Was a strict xfail until 24 Sep, measured then: whitelisting the reference
    half had fixed `no_invented_quantity`, and `no_price_quote` still replaced
    the answer with SAFE_FALLBACK because it cannot tell a subsidy the client
    wrote down from a price the model made up. Closed by exempting a price
    phrase that appears VERBATIM in the reference row served this turn --
    `guardrails._quoted_from`. It went XPASS(strict) the moment that landed,
    which is the only reason the marker is gone.
    """
    conv = Conversation(reference_text=ANSWER_BANK)
    heard = await conv.say("సబ్సిడీ ఎంత వస్తుంది అండి?", SUBSIDY)
    assert heard.strip() == SUBSIDY.strip(), (
        f"the client's own subsidy figures were replaced: {heard!r}")


# --- one last sweep over every well-behaved scenario -------------------------
async def test_no_scenario_ever_lets_the_caller_hear_a_fragment():
    """Run 783's "అర్థమైంది బిల్లు?", applied to every conversation at once.

    Honest about its reach: the splice path this rule guards was removed before
    this file existed -- `_gate` refuses to substitute once `_spoken` is
    non-empty -- so this test does NOT fail on today's code and cannot be shown
    failing without reverting that. It is here so the guarantee cannot be traded
    away again silently, and the oracle itself is proved against the recorded
    fragment in `test_the_premises_of_every_fixture_below_hold`.
    """
    conv = Conversation(known={"roof_available": "true"}, variants=VARIANTS,
                        reference_text=ANSWER_BANK)
    await conv.say("చెప్పండి", "నమస్కారం అండి, " + Q["location"], chunk=4)
    await conv.say("విజయవాడ.", "సరే అండి, " + Q["monthly_bill"],
                   learns={"location": "విజయవాడ"}, chunk=6)
    await conv.say("యాభై వేలు అండి.", "మంచిది, " + Q["roof_available"])
    await conv.say("వారంటీ ఎంత?", WARRANTY, chunk=12)
    await conv.say("హలో")

    assert not no_fragments(conv), f"fragments heard: {no_fragments(conv)}"
    assert not no_known_field_asked_again(conv)
    assert len(distinct_questions_asked(conv)) <= len(Q)
