"""The live state block -- what replaces the node graph.

A node graph makes the state machine GENERATE the reply, which is backwards:
when the caller says something no edge matches, there is no transition and the
agent goes silent. That is the Dograh bug, and it is structural.

Here the state machine only CONSTRAINS. The model always generates, so there is
never an edge to fall off. This block is ~80 tokens injected fresh each turn --
it tells the agent where it is and what it still owes, without ever blocking it
from answering whatever was actually said.
"""

from __future__ import annotations

import re
import time
from difflib import SequenceMatcher
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum

from api.services.vaani import amounts, booking, coach
from api.services.vaani.corrections import is_correction


class Phase(Enum):
    OPENING = "opening"
    QUALIFYING = "qualifying"
    PITCHING = "pitching"
    CLOSING = "closing"
    WRAPPING = "wrapping"


# Telugu marks a yes/no question by suffixing the AA vowel sign to the verb --
# వస్తుందా, దొరుకుతుందా, పెట్టాలా -- with no "?" and no change in
# word order. Looking for "?" or wh-words alone caught 5 of 8 real caller
# questions and missed exactly the ones that matter: "will I get power in the
# rainy season", "can I get a loan", "do I need a battery".
#
# The statement forms end in a different vowel sign (వస్తుంది "it comes"),
# so the ending is what separates them.
_QUESTION_WORDS = re.compile(
    r"(\?|ఎంత|ఎక్కడ|ఎప్పుడు|ఎలా|ఏమిటి|ఏంటి|ఏమి|ఎందుకు|ఎవరు|ఎన్ని|ఏది|ఏవి|"
    r"\b(what|when|where|how|why|which|who|can|do|does|is|are)\b)",
    re.IGNORECASE)

# Ends on the AA sign: the Telugu interrogative particle.
_QUESTION_PARTICLE = re.compile(r"[ఀ-౿]ా\s*[?.!]?\s*$")

# The particle also detaches into its own word, and then the sentence carries on
# past it. Run 218 turn 18: "కాదు పాసిబుల్ అయి ఉంది యా బికాజ్ ఇట్స్ నాట్ ఏ స్మాల్
# ప్లాట్ ఇట్స్ లైక్ బిగ్ వన్." -- a question about whether solar is possible at
# all, ending in a full stop, with the interrogative sitting in the middle. Both
# of the patterns above miss it, and the agent answered by asking his name.
#
# Only "యా". Bare "ఆ" is excluded deliberately: it is both the filler "ah" that
# opens half this caller's turns and the demonstrative "that" -- "ఆ కంపెనీ మీద
# పెట్టాలి" (install it on that company) is a statement, and reading it as a
# question would suppress the checklist and stall the call outright.
_QUESTION_CLITIC = re.compile(r"\S\s+యా(\s|[?.!,]|$)")

# English asked inside a Telugu sentence, which is how this caller asks the
# things he most wants answered. "possible" is the specific word run 218 turned
# on and the reason it is here by name.
_QUESTION_EN = re.compile(
    r"\b(possible|available|worth it|how much|how many|what about|"
    r"tell me|explain|any idea|will (it|you|i)|should i)\b", re.IGNORECASE)


def _norm(text: str) -> str:
    """Strip punctuation and spacing so two spellings of one sentence compare equal."""
    return re.sub(r"[^\wఀ-౿]+", "", (text or "").lower())


def echoes_agent(text: str, spoken: list) -> bool:
    """Is this the agent's own voice coming back down the line?

    Run 270 is a call with no human content in it at all:

        AGENT   సరే, మీ పేరు,        CALLER  సరే, మీ పేరు?
        AGENT   మీ నెల బిల్లు ఎంత     CALLER  మీ నెల బిల్లు
        AGENT   మంచిది, మీ           CALLER  మంచిది.

    Every "caller" line is the sentence the agent had just spoken. Run 261 was
    the same, and was proved acoustically: the caller track matched the agent
    track delayed 300ms, correlating 0.88 on the envelope. The phone is on
    speakerphone and hears itself.

    Left alone this runs away. The agent speaks, hears itself, treats it as an
    interruption, abandons its sentence, answers itself, and hears that too --
    which is why run 270 collapsed into "మంచిది / మంచిది / సరే / సరే" and never
    reached a single real question.

    Telling the client not to use speakerphone is not a fix. Customers will, and
    the agent has to survive it. This cannot stop the audio arriving, but it can
    stop the agent TREATING it as the caller, which is the part that runs away.

    Compared against the agent's recent utterances only, and only for a
    containment match: echo is a prefix of what was said, because the agent gets
    cut off partway through hearing itself.
    """
    t = _norm(text)
    if len(t) < 4:
        # Too short to attribute. "ఆ" is both an echo fragment and a real
        # backchannel, and silencing real callers is worse than hearing an echo.
        return False
    for said in list(spoken)[-3:]:
        s_norm = _norm(said)
        if not s_norm:
            continue
        if t in s_norm or s_norm in t:
            return True
        # Echo comes back through the speaker, the room and the phone codec, so
        # the STT mishears the tail of it: run 270's "మీకు మీ స్వంత" returned as
        # "మీకు మీ సూచి". Containment misses that; similarity does not.
        #
        # 0.55 sits in a measured gap, not a guessed one. Scored against every
        # echo line in run 270 and every real answer in run 269:
        #
        #     echo lines        0.67 - 1.00
        #     real answers      0.11 - 0.44
        #
        # Nothing lands between 0.44 and 0.67, so the threshold has room on both
        # sides rather than being tuned to the edge of the data.
        head = s_norm[:len(t) + 6]
        if SequenceMatcher(None, t, head).ratio() >= 0.55:
            return True
    return False


def _is_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(
        _QUESTION_WORDS.search(t)
        or _QUESTION_PARTICLE.search(t)
        or _QUESTION_CLITIC.search(t)
        or _QUESTION_EN.search(t)
    )


# Fields that mean "agree a visit". These get concrete slots instead of a
# yes/no question, because run 262 answered the yes/no perfectly and still left
# nobody knowing when to turn up.
MONEY_FIELDS = ("bill", "amount", "spend", "budget", "consumption")


def _is_money_field(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in MONEY_FIELDS)


BOOKING_FIELDS = ("assessment_agreed", "appointment", "callback", "visit",
                  "site_visit", "schedule")


def _is_booking_field(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in BOOKING_FIELDS)


@dataclass
class CallState:
    required_fields: list[str] = field(default_factory=list)
    questions: dict = field(default_factory=dict)   # field -> the actual question
    known: dict[str, str] = field(default_factory=dict)
    objections: list[str] = field(default_factory=list)
    phase: Phase = Phase.OPENING
    turn: int = 0
    started_at: float = field(default_factory=time.time)
    disqualified: bool = False
    disqualify_reason: str = ""
    # Set when the agent has just told the caller it could not hear him.
    #
    # Run 312 said "మీరు చెప్పినది బాగా వినిపించలేదు", got another garbled
    # answer, read it as "no roof", and ended the call on a factory owner. An
    # utterance the agent has already admitted it could not parse is not
    # evidence for anything, least of all for hanging up. See
    # `extractor.apply_to_state`.
    misheard_last_turn: bool = False
    next_step_agreed: bool = False   # a visit/callback/time has been accepted
    buying_signal: bool = False      # caller asked to book, or asked a closing question
    refusals: int = 0                # plain refusals so far; the 2nd ends the call
    no_more_questions: bool = False  # caller explicitly asked to stop being asked
    must_end: bool = False           # removal requested, hostile, or fraud accusation
    end_reason: str = ""

    # A field asked this many times is abandoned, answered or not.
    #
    # Run 218: the caller gave his bill on turn 2 ("టెన్ టు ట్వంటీ లాక్స్") and
    # was asked for it again on turns 3, 15 and 17, because extraction lands a
    # turn late and STILL_NEED had not caught up. He replied
    # "చెప్పాను కదా అప్పుడే" (I already told you), then
    # "ఎన్ని సార్లు అడుగుతారు?" (how many times are you going to ask?), then
    # "మీరు చాలా ఇన్‌కన్సిస్టెంట్ గా", and ended the call.
    #
    # Rewording is why the repeat guard missed it -- "బిల్లు ఎంత?" and
    # "బిల్లు సుమారు ఎంత?" are different sentences asking the identical thing.
    # Counting the FIELD instead of comparing the words does not care how it is
    # phrased. Two attempts is the whole budget: one ask, one clarification.
    MAX_ASKS_PER_FIELD = 2

    @property
    def still_need(self) -> list[str]:
        return [f for f in self.required_fields
                if f not in self.known
                and self.ask_counts.get(f, 0) < self.MAX_ASKS_PER_FIELD]

    @property
    def abandoned(self) -> list[str]:
        """Fields given up on. Better an unknown than a caller hanging up."""
        return [f for f in self.required_fields
                if f not in self.known
                and self.ask_counts.get(f, 0) >= self.MAX_ASKS_PER_FIELD]

    def note_amount(self, text: str) -> bool:
        """Record the bill the moment it is said, not a turn later.

        Run 274: the caller said "వన్ లాక్ అండి" twice and was asked the same
        question three times, because the amount was left to the asynchronous
        extractor -- which answers a turn late and answered null anyway. Money
        is structured; it is read here, synchronously, before the reply.
        """
        asking = bool(self.still_need) and _is_money_field(self.still_need[0])
        # A caller correcting himself. "సారీ, పది కాదు -- ఇరవై లక్షలు" arrives
        # after the field is already filled, so the gate below would drop it and
        # the lead record would keep the figure he just told us was wrong.
        # Opening the gate to any later number is what booked run 266's phantom
        # appointment; opening it only to an explicit repair keeps that shut.
        revising = (not asking and is_correction(text)
                    and any(_is_money_field(f) for f in self.known))
        if not asking and not revising:
            # Only while a bill is actually being asked for. Otherwise "మూడు
            # లక్షలు" said in passing would overwrite a confirmed figure --
            # the same class of bug that once booked an appointment from it.
            return False
        amount = amounts.parse_amount(text)
        if amount is None:
            # A repair with no new figure in it is not yet a repair -- he is
            # about to say the number. Nothing is unset on the strength of a
            # "కాదు" alone.
            return False
        field = (self.still_need[0] if asking else
                 next(f for f in self.known if _is_money_field(f)))
        if not amount.plausible:
            # Heard, but not believed. Run 286's caller said "60 క్రోర్స్" and
            # was congratulated on it. Recording the figure would put a fiction
            # in the lead record; ignoring it silently would ask the same
            # question again. So it is neither: the state block asks him to
            # confirm, once.
            self.doubted = amount
            return False
        self.known[field] = str(amount.rupees)
        self.amount = amount
        if revising:
            # So the reply says it back. A correction the caller cannot hear
            # land is indistinguishable to him from one that was ignored.
            self.corrected = amount
            self.reacted = False
        return True

    def note_booking(self, text: str) -> bool:
        """Record an appointment, but only if the caller actually named one.

        Consent is not a time. Run 262's caller said "ఆ బాగుంటుంది, ఓకే" to a
        menu of two and the agent booked the first one for him. Returning False
        here is what makes the agent ask which, instead of guessing.
        """
        if self.appointment_iso or booking.declined(text):
            return False
        # Nothing is a time until a time has been ASKED for.
        #
        # Run 266 is why this gate exists. The parser ran on every caller
        # utterance, so "మూడు లక్షలు" -- an answer to the BILL question, on turn
        # three of a qualification call -- was read as 3 oclock. The agent
        # announced a booking, saved 2026-08-29T15:00, and ended the call after
        # 38 seconds. The caller had never been offered a slot at all.
        #
        # A number only becomes a time once the agent has put two slots to them
        # and is waiting for an answer. Before that there is no question a time
        # could be the answer to.
        # ...unless the utterance is a time on its own terms. Run 323's caller
        # was asked "what time suits you?" and answered "ఎల్లుండి సాయంత్రం ఐదు
        # ఇంటికి" before any menu had been rendered, so `offered` was empty and
        # his answer was thrown away. The agent then told him his time was not
        # among its options and booked him for today at four -- two days out,
        # agreed by both, noticed by neither. A day word plus a named hour is an
        # appointment however it arrives; see booking.names_a_time_unprompted.
        if not self.offered and not booking.names_a_time_unprompted(text):
            return False
        # The menu settles the day when they answer with the hour alone.
        when = booking.parse_slot(text, offered=self.offered)
        if when is None:
            return False
        if booking.is_taken(when, self.taken_slots):
            # Somebody else already has it. Saying yes would stand one of them
            # up, so treat it as "not chosen" -- the caller is asked again, and
            # the offers no longer include it.
            self.offered = ()
            return False
        self.appointment_iso = when.isoformat()
        return True

    def note_day_rejected(self, text: str) -> bool:
        """A day the caller has ruled out never appears in an offer again.

        Run 336: "నాకు అసలుకి రేపు సరిపోదండి" -- tomorrow does not suit me at
        all -- and the very next menu led with tomorrow. Re-offering a day that
        was just refused is the clearest possible signal that nobody is
        listening.

        Implemented by marking that day's slots TAKEN, because `offer_slots`
        already excludes taken slots and already searches forward for the next
        free pair. A rejected day and a day promised to somebody else are the
        same thing from the menu's point of view.
        """
        day = booking.rejected_day(text)
        if day is None:
            return False
        blocked = [day.replace(hour=h).isoformat() for h in booking.OFFER_HOURS]
        fresh = [b for b in blocked if b not in self.taken_slots]
        if not fresh:
            return False
        self.taken_slots.extend(fresh)
        # Rebuild the menu, since the one in hand may lead with that day.
        self.offered = ()
        return True

    def offer_line(self) -> str:
        """The two times, in words, ready to be put to the caller.

        Every branch that mentions offering a time uses this. Run 305 is what
        happens otherwise: the caller said "ఆ ఉంది" (yes, I have time), which
        set `buying_signal`, and that branch read

            "CALLER IS READY TO BOOK. Stop qualifying. Offer a specific time
             and close."

        -- an instruction to name a time, with no time in it. The block that
        holds the real slots sat further down the same elif chain and was never
        reached. So the model invented them, and then invented different ones
        twice more:

            రేపు సాయంత్రం ఐదు గంటలకు లేదా శుక్రవారం ఉదయం పది గంటలకు
            రేపు సాయంత్రం ఐదు, శుక్రవారం ఉదయం పది, లేదా శనివారం మధ్యాహ్నం మూడు
            ఉదయం తొమ్మిది, మధ్యాహ్నం ఒక గంట, లేదా సాయంత్రం ఐదు

        Three different menus in twenty seconds, none of them from the booking
        system, and the slot the caller finally chose existed nowhere. A
        confirmed appointment nobody can honour is worse than no appointment:
        the customer waits in and the vendor never comes.
        """
        if not self.offered:
            self.offered = booking.offer_slots(taken=self.taken_slots)
        first, second = self.offered
        # Two failures this line has actually produced, both on 30 August:
        #
        # Run 322 asked "ఈ రెండు ఎంపికలలో ఏది మీకు బాగుంటుంది?" -- which of these
        # two suits you -- having never said either of them. The caller repeated
        # his answer verbatim because there was nothing to choose between.
        #
        # Run 323 read "invent no other time" as a rule about the CALLER and
        # told a man who had named a perfectly workable five o'clock that his
        # time "మా ఎంపికలలో లేదు" -- is not among our options. It is the agent
        # that may not invent a time. A customer may say any time he likes.
        return (f'SAY BOTH OF THESE TIMES ALOUD: "{first.say()}" or '
                f'"{second.say()}" -- never say "the two options" without '
                "saying them. Invent no time of your own; but ACCEPT any time "
                "THEY name and never tell them a time is unavailable. A bare "
                "yes is not a booking -- ask WHICH.")

    def note_reschedule(self, text: str) -> bool:
        """The caller wanting a different time after one is already booked.

        Run 300, the last forty seconds of an otherwise good call:

            AGENT   ఎల్లుండి సాయంత్రం four oclockకి షెడ్యూల్ చేసాం. ధన్యవాదాలు...
            CALLER  రేపు ఏమైనా ఉన్నాయా స్లాట్స్?        any slots tomorrow?
            AGENT   ఎల్లుండి సాయంత్రం four oclockకి... (identical)
            CALLER  రేపు స్టార్ట్స్ ఉన్నాయా ఏమన్నావ్     what did you say?
            AGENT   ...(identical)
            CALLER  కాదు కాదు రేపు ఉన్నాయా. రేపు. రేపు ఉన్నాయా?
            AGENT   ...(identical)

        Four times. The BOOKED branch of the state block withdraws the whole
        checklist and instructs one closing sentence, which the model then
        produced on every turn no matter what was said to it -- so the more
        insistently he asked, the more exactly it repeated itself.

        A booking is not the end of the conversation, and treating it as one
        makes the agent deaf at the single most expensive moment of the call:
        a customer who wants an earlier slot is a customer who wants to buy.

        The same rule as the money correction, for the same reason. A bare later
        time may not silently move an appointment -- that is how run 266 booked
        a visit out of "మూడు లక్షలు". But a caller who NAMES a different time is
        rebooked, and a caller who ASKS about another day has the slots reopened
        and put to him again rather than being guessed at: he asked whether
        tomorrow was possible, which is not the same as choosing 10 a.m.
        """
        if not self.appointment_iso:
            return False
        # The menu is still in hand after a booking, and a caller moving the
        # visit names the new hour the same way he named the first one.
        when = booking.parse_slot(text, offered=self.offered)
        if when is None and not is_correction(text):
            # Nothing about time in it at all. Not this function's business.
            return False

        if _is_question(text):
            # Asking, not choosing. Reopen and offer; never pick for them.
            self.appointment_iso = ""
            self.offered = ()
            self.rebooked = None
            return True

        # A statement moves the visit only if it also ASKS to. Naming a day in
        # passing is not a request -- "ఎల్లుండి మా వాళ్ళు ఊరికి వెళ్తున్నారు"
        # mentions a day and asks for nothing, and moving a confirmed
        # appointment on the strength of it is run 266's bug wearing a
        # different hat.
        if not (is_correction(text) or booking.RESCHEDULE.search(text or "")):
            return False
        booked = datetime.fromisoformat(self.appointment_iso)
        if when is None or when == booked or booking.is_taken(when, self.taken_slots):
            return False
        self.appointment_iso = when.isoformat()
        self.rebooked = booking.Slot(when)
        return True

    def commit_ask(self) -> None:
        """Spend one ask, at the moment the agent actually says it.

        Counted on speech rather than on prompt-building because they are not
        the same event. Run 218's caller interrupted constantly, and every
        fragment -- "హలో", a cough, a half word -- rebuilds the prompt. Counting
        there would burn a field's whole budget on interjections the agent never
        answered, and drop a question that was never actually put to him.
        """
        if self.pending_ask:
            self.ask_counts[self.pending_ask] = (
                self.ask_counts.get(self.pending_ask, 0) + 1)
            self.last_asked = self.pending_ask
            self.pending_ask = ""

    @property
    def elapsed_s(self) -> int:
        return int(time.time() - self.started_at)

    # Fields that must survive a turn boundary. Everything else is either
    # rebuilt per turn (pending_ask, last_user_text) or configuration
    # (required_fields, questions) that comes from the workflow.
    #
    # A phone call keeps ONE CallState for its whole length, so this never
    # mattered there. Text chat does not: `text_chat_runner` builds a fresh
    # pipeline for every message and restores only `messages`,
    # `gathered_context` and `tool_state` from its checkpoint. The Vaani brain
    # was therefore starting from zero on every single turn, which means
    # `ask_counts` was always empty and MAX_ASKS_PER_FIELD could never bite,
    # and `asked` was always empty so `_is_repeat` could never fire.
    #
    # Probed 4 Sep: wf3 asked its question on five consecutive turns and wf6 on
    # three, with no repair line and no abandonment. Both are the client's
    # "looping questions again and again". The eval battery runs on text chat
    # too, so its repetition failures have been measuring this and not the
    # agent.
    PERSISTED = (
        "known", "ask_counts", "asked", "last_asked", "objections", "turn",
        "disqualified", "disqualify_reason", "misheard_last_turn",
        "next_step_agreed", "buying_signal", "refusals", "no_more_questions",
        "must_end", "end_reason", "appointment_iso", "taken_slots", "reacted",
    )

    def snapshot(self) -> dict:
        """The part of the state a later turn needs, JSON-safe."""
        return {k: getattr(self, k) for k in self.PERSISTED
                if getattr(self, k, None) not in (None, "", 0, [], {})}

    def restore(self, data: dict | None) -> None:
        """Re-seed from `snapshot`. Unknown keys are ignored, not trusted."""
        if not isinstance(data, dict):
            return
        for k in self.PERSISTED:
            if k in data:
                setattr(self, k, data[k])

    def learn(self, field_name: str, value: str) -> None:
        if value:
            self.known[field_name] = value

    def note_objection(self, kind: str) -> None:
        if kind not in self.objections:
            self.objections.append(kind)

    # What the caller said on the turn just gone, so the acknowledgement has
    # something concrete to refer to instead of being generic.
    last_user_text: str = ""
    # How many times each field has been asked for, so a caller is never
    # interrogated about the same thing a third time.
    ask_counts: dict = field(default_factory=dict)
    # The field the current prompt nominates, not yet spoken.
    pending_ask: str = ""
    # The field whose question was most recently SPOKEN. `pending_ask` is
    # cleared the moment it is committed, so by the time the caller answers
    # there is nothing left naming what he was answering -- which is what a
    # refund has to know. Cleared when refunded, so one interruption cannot
    # give back two asks.
    last_asked: str = ""
    # The appointment, once the caller has named one. ISO, because a vendor
    # diary needs a timestamp and not a sentence.
    appointment_iso: str = ""
    # The two slots offered, held so a later "the first one" can be resolved
    # and so the agent never quietly re-offers different times mid-call.
    offered: tuple = ()
    # Appointments already promised to other callers, so two customers are never
    # given the same slot. Populated at call start; empty means "unknown", which
    # degrades to today's behaviour rather than blocking a booking.
    taken_slots: list = field(default_factory=list)
    # The parsed bill, kept so the reply can react to its SIZE rather than just
    # recording it. A factory owner quoting 50 lakhs and a household quoting
    # 2,000 are not the same conversation.
    amount: object = None
    # One reaction per call. Repeating "that is a big bill" every turn is the
    # opposite of sounding human.
    reacted: bool = False
    # An amount that was said but is not credible as a monthly bill.
    doubted: object = None
    # A figure the caller revised this turn, so the reply confirms the new one.
    corrected: object = None
    # A time the caller moved this turn, so the reply confirms the new one.
    rebooked: object = None
    # The caller has asked to be heard THIS turn. One turn only: he asked for
    # the floor, he gets the floor, and then the call carries on.
    wants_the_floor: bool = False
    # Coaching cues already spent this call. Layer 2's "one rebuttal,
    # never two" applies to the prompt as much as to the agent:
    # re-injecting the same tactic on a caller who did not take it the
    # first time is the prompt itself pushing a second time.
    coached: set = field(default_factory=set)
    # What WE have already asked. Run 96 asked the same question four times and
    # the caller said "you told me nothing"; the model cannot avoid repeating
    # itself if it is never shown what it already said.
    asked: list = field(default_factory=list)

    def advance(self) -> None:
        """Move the phase forward based on what we actually know.

        Deliberately simple and deterministic. The agent decides what to SAY;
        this only tracks where the call has got to.
        """
        self.turn += 1
        if self.disqualified or self.next_step_agreed or self.must_end:
            self.phase = Phase.WRAPPING
        elif self.phase is Phase.OPENING and self.turn >= 1:
            self.phase = Phase.QUALIFYING
        elif self.phase is Phase.QUALIFYING and not self.still_need:
            self.phase = Phase.PITCHING
        elif self.phase is Phase.PITCHING and self.turn >= 8:
            self.phase = Phase.CLOSING

    def render(self) -> str:
        """The compact block injected into the prompt each turn.

        This block is the LAST thing the model sees, which makes it the most
        authoritative thing in the context -- more so than 3,000 tokens of prose
        further up. That is exactly why the hard behavioural constraints live
        here and not only in the prompt layers: listing STILL_NEED at the end of
        the context reliably makes the model ask for those fields, even when the
        prose says not to. So once the call is won or lost, we stop listing them.
        """
        lines = [f"PHASE: {self.phase.value}", f"KNOWN: {self.known or '{}'}"]

        # Run 298. The caller asked what bill was meant, and the agent explained:
        #
        #     "అది మీ ఇంటి నెలవారీ విద్యుత్ బిల్లు"   that is your HOUSE's monthly bill
        #
        # Nobody had said anything about a house. `property_type` was null and
        # had not been asked; the man could have been ringing from a factory, a
        # shop or a rented room. The model filled the gap because a fluent
        # sentence wants a noun there, and an invented one reads as fluent.
        #
        # KNOWN above says what we have. It does not say that everything else is
        # unknown, and a model reading a list of facts does not infer the
        # absence of the others -- it infers the most ordinary case. So the
        # absence is stated outright.
        #
        # Deliberately not a rule about houses. The same failure would put a
        # city, a budget or a name into the agent's mouth just as readily, and a
        # salesperson who invents the customer's situation has stopped listening
        # to them -- which is the complaint this whole file exists to answer.
        unknown = [f for f in self.required_fields if f not in self.known]
        if unknown:
            # Kept to one clause on purpose. The first version of this line ran
            # to 279 characters -- 59% of the whole state block -- and the block
            # is the UNCACHED tail, re-read and re-billed on every single turn.
            # Run 300 measured the LLM at 0.325s against run 292's 0.235s, and
            # this line was most of the difference. The instruction is what
            # works; the explanation of it was being paid for sixteen times a
            # call and read by nobody.
            lines.append(f"NOT TOLD YET: {unknown} -- never state or imply one.")

        # Any ending state MUST suppress the checklist. The 30-persona run showed
        # that simply listing STILL_NEED at the end of the context makes the model
        # ask for those fields -- even right after it agreed to remove the caller
        # from the list. That produced 8 of 12 compliance violations.
        if self.must_end:
            lines.append(f"STILL_NEED: [] -- STOP. {self.end_reason} "
                         "Say one short closing sentence and END THE CALL. "
                         "Ask NOTHING. Pitch NOTHING.")
        elif self.disqualified:
            lines.append("STILL_NEED: [] -- DISQUALIFIED. Do not ask anything "
                         "further and do not sell. Close warmly in one sentence.")
        elif self.next_step_agreed and not self.appointment_iso:
            # Agreeing to a visit is not a visit. Ending here leaves the vendor
            # with a lead and no time to turn up at, so the times come first.
            lines.append("STILL_NEED: [] -- THEY AGREED TO THE VISIT but no "
                         "time is fixed yet. " + self.offer_line())
        elif self.next_step_agreed:
            lines.append("STILL_NEED: [] -- NEXT STEP IS AGREED. Do not ask "
                         "anything further. Thank them and end the call NOW. "
                         "Remaining details are collected at the visit.")
        elif self.buying_signal:
            lines.append("STILL_NEED: [] -- CALLER IS READY TO BOOK. Stop "
                         "qualifying. " + self.offer_line())
        elif self.appointment_iso:
            # Booked. Everything else is now a reason to lose it.
            when = booking.Slot(datetime.fromisoformat(self.appointment_iso))
            if self.rebooked is not None:
                said = self.rebooked.say()
                self.rebooked = None
                lines.append(
                    f'STILL_NEED: [] -- THEY MOVED IT. The visit is now "{said}", '
                    "not the earlier time. Say those exact words back to them, "
                    "the day included, thank them, and END THE CALL.")
            elif _is_question(self.last_user_text):
                # Booked is not deaf. Run 300 repeated one closing sentence at a
                # man asking about another day, four times, until he gave up.
                lines.append(
                    f'STILL_NEED: [] -- BOOKED for "{when.say()}". THEY JUST ASKED '
                    "YOU SOMETHING: answer THAT first, in one short sentence. "
                    "Then say that time back in those exact words, the day "
                    "included, and END THE CALL.")
            else:
                # The DAY is the half that goes missing. Run 323 booked
                # "ఎల్లుండి సాయంత్రం five o'clock" and said back "సాయంత్రం ఐదు
                # గంటలకు" -- the hour, alone, with no day attached to it. A time
                # without a day is not an appointment; it is something the
                # caller will remember differently from the vendor.
                lines.append(
                    f'STILL_NEED: [] -- BOOKED for "{when.say()}". Say those '
                    "exact words back once, the day included, so they can "
                    "correct it, thank them, and END THE CALL. Ask nothing "
                    "further.")
        elif self.no_more_questions:
            lines.append("STILL_NEED: [] -- THE CALLER HAS ASKED YOU TO STOP "
                         "ASKING QUESTIONS. Ask nothing at all. Answer what "
                         "they raised, or offer a time. Nothing else. "
                         + self.offer_line())
        elif self.wants_the_floor:
            # Run 324. He said "నేను చెప్తే వినండి" -- listen to what I say --
            # and was asked the same question again, then a different one, and
            # his answer was never captured.
            #
            # The checklist is suppressed for exactly one turn, the same way it
            # is for a question, and for the same reason: prose telling the
            # model to wait does not beat a list of fields at the end of the
            # context. Removing the list does.
            self.wants_the_floor = False
            self.pending_ask = ""
            lines.append(
                "STILL_NEED: [] -- HE ASKED YOU TO LISTEN. Ask NOTHING this "
                "turn. Say one short line inviting him to go on -- చెప్పండి "
                "సార్, వింటున్నాను -- and then STOP TALKING. Do not fill the "
                "silence and do not move to another question.")
        elif _is_question(self.last_user_text):
            # THE CHECKLIST IS SUPPRESSED, and that is the entire point.
            #
            # Answering first was already instructed here, as a line sitting
            # underneath STILL_NEED and NEXT QUESTION TO ASK. Run 218 shows what
            # that is worth. Turn 18, the caller asks whether solar is even
            # possible on a plot that size -- "ఇట్స్ నాట్ ఏ స్మాల్ ప్లాట్, ఇట్స్
            # లైక్ బిగ్ వన్" -- and the agent replies "సారీ, మీ పేరు చెప్పగలరా?".
            # By turn 36 he has spelled it out: "అతను సోలార్ పెట్టొచ్చా అని
            # అడిగాను, మీరేమో పేరు అడుగుతున్నారు" -- I asked whether solar can be
            # installed, and you are asking my name.
            #
            # The detector was not the failure. It fired on turns 10 and 13 and
            # the agent asked its own question anyway, because three lines told
            # it to ask and one told it to answer. This module's own docstring
            # says why: listing STILL_NEED at the end of the context makes the
            # model ask for those fields even when the prose forbids it. Prose
            # does not beat the checklist. Removing the checklist does.
            #
            # ONE ORDERED INSTRUCTION, not a checklist and not two competing
            # lines. That distinction is the whole of run 218's lesson: the
            # failure there was a bare "ASK THIS" sitting under a LIST OF
            # FIELDS, with the answer-first rule as prose underneath. The model
            # obeyed the list.
            #
            # But withdrawing the question entirely, which is what this branch
            # used to do, has its own failure and the client named it on 4 Sep:
            # "it should not skip questions ... first it need to answer user
            # question ... after that it need to get answer for all the
            # questions". Measured on run 575: he asked four things, was
            # answered correctly all four times, and the agent asked its own
            # question on ONE of those turns. The call simply stalls -- every
            # turn the caller is curious, the qualification stops dead.
            #
            # So the question comes back, SECOND, named in full, and explicitly
            # subordinate to the answer. The checklist stays withdrawn.
            #
            # The escape hatch matters as much as the rule: if answering
            # honestly takes the whole reply, asking as well would produce
            # exactly the interrogation run 218 complained about. So a long
            # answer is allowed to stand alone.
            nxt = self.still_need[0] if self.still_need else ""
            follow = ""
            if nxt and not _is_booking_field(nxt) and self.questions.get(nxt):
                follow = (
                    " THEN, in the SAME reply and only after you have actually "
                    f'answered, add this one question: "{self.questions[nxt]}" '
                    "-- do not reword it, do not drop the options, and never "
                    "ask it before the answer. If your answer needed more than "
                    "two sentences, leave the question out entirely.")
                self.pending_ask = nxt
            else:
                self.pending_ask = ""
            lines.append(
                "STILL_NEED: [] -- THE CALLER ASKED YOU SOMETHING. FIRST answer "
                "THAT, properly. Answer from your facts, or say the team will "
                "confirm the exact figure. Never invent a number, price, "
                "location or brand." + follow)
        elif not self.still_need:
            # Every field is either answered or out of its two-ask budget.
            #
            # Run 318 reached this state at 04:10 and then offered the same
            # appointment SIX more times, to a caller who was asking real
            # questions about cost and panels each time. The budget worked --
            # `assessment_agreed` had dropped out of the checklist. The problem
            # is what replaced it: the literal string "STILL_NEED: []" and
            # nothing else.
            #
            # An empty checklist is not an instruction. With no instruction the
            # model falls back on the conversation history, and by then the
            # history was six appointment offers deep, so it produced a seventh.
            # That is what the client means by harassing: an agent with nothing
            # left to ask, asking the last thing again.
            lines.append(
                "STILL_NEED: [] -- NOTHING LEFT TO ASK. You have already "
                "offered a time; do NOT offer one again and do NOT re-ask "
                "anything. Answer whatever they ask, warmly and in one or two "
                "sentences. When they have no more questions, thank them by "
                "name and END THE CALL.")
        else:
            lines.append(f"STILL_NEED: {self.still_need or '[]'}")
            # Field KEYS are meaningless to the model -- it was being handed
            # `save_with_any` and left to invent a question from it. Spell out
            # the next one in the client's own words.
            nxt = self.still_need[0] if self.still_need else ""
            if nxt and _is_booking_field(nxt):
                # Booking is not a yes/no question. Run 262 asked one, got a
                # clean "yes", and stored `assessment_agreed: true` with no day
                # and no time -- so the vendor had nothing to act on and the
                # caller had been told a time that existed only in a transcript.
                lines.append(self.offer_line())
                self.pending_ask = nxt
            elif nxt and self.questions.get(nxt):
                # "TO ASK" was read as a topic rather than a script. Run 336
                # was handed `"మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్
                # ప్లేసా?"` and said "మీ ప్రాపర్టీ టైప్ ఏది?" instead -- the
                # schema's field name, translated live, with the options thrown
                # away. The caller answered "ఏం చెప్పాలి, నాకు తెలీదు".
                #
                # The client writes these questions; they name the options on
                # purpose, because a caller told the options answers in one word.
                lines.append(
                    f'ASK THIS, IN THESE EXACT WORDS: "{self.questions[nxt]}" '
                    "-- do not reword it and do not drop the options.")
                self.pending_ask = nxt
                # The client's complaint, in one word: "no confirmations". The
                # reference agent opens nearly every turn with a two-word
                # acknowledgement -- "మంచిది", "సరేనండి", "చాలా సంతోషమండి" --
                # before asking anything. Ours went straight to the next
                # question and the caller said "you told me nothing".
                #
                # It lives here rather than in the prose layers because this
                # block is the last thing the model reads, and the same
                # instruction has been in Layer 2 all along without being obeyed.
                # Capped at two words on purpose: a long acknowledgement is
                # audio the caller waits through on every single turn.
                # Every line here is re-read and re-reasoned on EVERY turn,
                # so wording costs latency directly. The first version of these
                # three rules ran to 1,086 characters and took the LLM's first
                # token from 0.22s to 0.655s -- total 1.27s to 2.10s on run 206.
                # Same rules, said once.
                # Saying "I could not hear you" and then asking something
                # ELSE is how run 314 stored "సంతై" as a caller's city while he
                # was saying "మంచిర్యాల్" for the second time. One line, because
                # every line here is re-read on every turn and costs latency
                # directly -- and it only appears on the turn after a misheard
                # one, not on all of them.
                if self.misheard_last_turn:
                    lines.append(
                        "YOU JUST SAID YOU COULD NOT HEAR THEM -- ask the SAME "
                        "question again, not a different one.")

                # An impossible figure gets questioned, not celebrated.
                if self.doubted is not None:
                    said = getattr(self.doubted, "say", lambda: "")()
                    alts = getattr(self.doubted, "alternatives", lambda: [])()
                    self.doubted = None
                    if alts:
                        # Name the alternatives. Run 312's caller said 2,000 and
                        # Sarvam returned 2 crore -- one misheard syllable,
                        # 10,000x. "Are you sure?" gets that syllable back
                        # unchanged; "thousands or crores?" is answered in one
                        # word and cannot be misheard the same way twice.
                        choices = " or ".join(alts + [said])
                        lines.append(
                            f"THEY SAID THEIR BILL IS {said} -- almost "
                            f"certainly misheard. Ask which they meant: "
                            f"{choices}. Do NOT agree with it and do NOT "
                            "praise it.")
                    else:
                        lines.append(
                            f"THEY SAID THEIR BILL IS {said} -- that cannot be "
                            "a monthly electricity bill. Say warmly that it "
                            "sounds much larger than usual and ask them to "
                            "confirm the monthly figure. Do NOT agree with it "
                            "and do NOT praise it.")

                # React to the SIZE of the bill, not just record it.
                #
                # A factory owner quoting 50 lakhs a month and a household
                # quoting 2,000 are not the same conversation, and answering
                # both with the same flat next-question is what makes this read
                # as a form rather than a person. Run 269's caller said "మాది
                # ఫ్యాక్టరీ" and got the identical script a household gets.
                #
                # Named bands, not a sliding scale: the model needs one clear
                # instruction, and every extra clause here is re-read and
                # re-billed on every turn (the state block is the uncached tail
                # -- 1,086 chars once cost 0.43s per turn).
                # A correction the caller cannot hear land is, to him,
                # indistinguishable from one that was ignored -- and being
                # ignored after taking the trouble to correct you is worse than
                # the original mistake. So the new figure is said back before
                # anything else happens with it.
                if self.corrected is not None:
                    was = getattr(self.corrected, "say", lambda: "")()
                    self.corrected = None
                    lines.append(
                        f"THEY JUST CORRECTED THEMSELVES -- the bill is {was}, "
                        "not what they said before. Say the new figure back so "
                        "they know you caught it, in one short clause, and do "
                        "NOT ask for it again.")
                if self.amount is not None and not self.reacted:
                    self.reacted = True
                    rupees = getattr(self.amount, "rupees", 0)
                    said = getattr(self.amount, "say", lambda: "")()
                    # Bands set against real Indian monthly electricity bills,
                    # not round numbers: 50,000/month is already a large
                    # commercial or large-home bill, and 20 lakhs is a factory.
                    if rupees >= 2_000_000:
                        lines.append(
                            f"THEIR BILL IS {said} -- very large, industrial "
                            "scale. Say that is a significant bill and this is "
                            "exactly the case solar pays back fastest on. Sound "
                            "impressed, briefly, then continue.")
                    elif rupees >= 50_000:
                        lines.append(
                            f"THEIR BILL IS {said} -- large. Acknowledge the "
                            "savings are substantial at that level, in one "
                            "clause, then continue.")
                    elif rupees < 3_000:
                        lines.append(
                            f"THEIR BILL IS {said} -- small. Do NOT oversell. "
                            "Be honest that savings scale with usage, stay warm, "
                            "and continue.")

                if self.asked:
                    lines.append(f"ALREADY SAID: {self.asked[-1][:60]!r} "
                                 "-- do not repeat it; say you could not hear.")
                if self.last_user_text:
                    lines.append(
                        f"THEY SAID: {self.last_user_text[:60]!r} "
                        "-- open with two words (సరే / మంచిది), then ask.")
        # Just-in-time coaching. See coach.py: the depth of the objection
        # and persuasion training lives OUT of the system prompt, so it
        # costs nothing on the turns it is not needed -- which is most of
        # them. Growing that catalogue never grows the prompt.
        for line in coach.coach(self.last_user_text, self.coached):
            lines.append(line)
            self.coached.add(line.split("(", 1)[1].split(")", 1)[0])
        lines.append(f"TURN: {self.turn}   CALL_ELAPSED: {self.elapsed_s}s")
        return "\n".join(lines)
