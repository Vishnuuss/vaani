"""Turning "yes, that works" into an actual appointment.

What run 262 did, and why it is not booking
--------------------------------------------
The agent offered two slots, the caller agreed, and the agent confirmed:

    AGENT   మీకు రేపు ఉదయం ten oclock లేదా ఈ రోజు afternoon two oclock ...?
    CALLER  ఆ బాగుంటుంది, ఓకే నాకైతే ఓకే
    AGENT   సరే సుబ్బరాజు, రేపు ఉదయం ten oclockకి మా వేండర్ వస్తారు

The saved record for that call reads `assessment_agreed: true` and nothing else.
No day, no time. Nobody can act on that: the vendor does not know when to go,
and the caller was told a time that exists only in a transcript.

Two separate defects, and the second is worse
----------------------------------------------
1. The agreed slot is never stored.
2. The caller said "that is fine" to a menu of TWO, which does not name either
   one -- and the agent silently chose the first and stated it as settled. That
   is not a booking, it is a guess presented as a fact, and the customer finds
   out when somebody turns up on the wrong day.

So acceptance and selection are kept apart here. "అలాగే" is consent to meet; it
is not a time. Only an utterance that identifies WHICH slot produces a booking,
and anything short of that asks once more instead of assuming.

Offers are concrete on purpose
------------------------------
"When would suit you?" makes the caller invent a format and produces answers
nobody can parse. The reference agent the client held up as the standard offers
a closed choice -- "ఉదయం, మధ్యాహ్నం, లేదా సాయంత్రం" -- and so does this.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from api.services.vaani.amounts import SCALES as _SCALES
from datetime import datetime, timedelta, timezone

# The customers are in India and so is the vendor who has to drive to the site.
IST = timezone(timedelta(hours=5, minutes=30))

# Nobody wants a site survey at 7am or after dark, and a solar roof survey needs
# daylight. Offers are clamped into this window.
FIRST_HOUR = 9
LAST_HOUR = 18

# The hours actually offered. Not every legal hour: a short, fixed menu keeps
# the two offers far apart and easy to distinguish over a phone line.
OFFER_HOURS = (10, 16)

# How far ahead to keep searching when slots are taken. Past this the lead has
# gone cold anyway, and a human should be rescheduling rather than the agent.
MAX_DAYS_AHEAD = 14

DAY_WORDS = {
    "today": 0, "ఈ రోజు": 0, "ఈరోజు": 0, "ఇవాళ": 0, "ఇయ్యాల": 0, "आज": 0,
    "tomorrow": 1, "రేపు": 1, "कल": 1,
    "day after": 2, "ఎల్లుండి": 2, "परसों": 2,
}

# Telugu callers say the hour in English -- "ten oclock", "two" -- while the
# part of day stays Telugu. Both halves are parsed independently for that reason.
PART_OF_DAY = {
    "ఉదయం": 10, "పొద్దున": 10, "పొద్దునే": 10, "morning": 10, "सुबह": 10,
    "మధ్యాహ్నం": 14, "afternoon": 14, "दोपहर": 14,
    "సాయంత్రం": 17, "సాయంకాలం": 17, "evening": 17, "शाम": 17,
}

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "ఒకటి": 1, "రెండు": 2, "మూడు": 3, "నాలుగు": 4, "ఐదు": 5, "ఆరు": 6,
    "ఏడు": 7, "ఎనిమిది": 8, "తొమ్మిది": 9, "పది": 10, "పదకొండు": 11,
    "పన్నెండు": 12,
    # English numerals in Telugu script, which is how these callers say the
    # hour -- and how THIS AGENT says it back to them. `Slot.say()` renders
    # 16:00 as "ఎల్లుండి సాయంత్రం four oclock"; run 300's caller repeated that
    # phrase word for word as "ఎల్లుండి సాయంత్రం ఫోర్ ఓ క్లాక్", Sarvam
    # transcribed the English "four" in Telugu script, and nothing here matched
    # it. `named` stayed None, so the hour fell back to the generic
    # సాయంత్రం = 17:00 and the visit was booked for FIVE.
    #
    # Stored 2026-08-31T17:00 against a spoken "four oclock": the vendor arrives
    # an hour after the customer expected, and neither of them ever sees the
    # discrepancy. Silent, and worse than an error.
    #
    # Morning hid it. ఉదయం defaults to 10 and callers pick "ten", so the
    # fallback happened to be right and the gap only bit in the evening.
    #
    # Same class as the money parser's, which already carries these forms --
    # see amounts.NUMERALS. Kept as its own table rather than imported: this one
    # must stop at twelve, because a clock does.
    "వన్": 1, "టు": 2, "త్రీ": 3, "ఫోర్": 4, "ఫైవ్": 5, "సిక్స్": 6,
    "సెవెన్": 7, "ఎయిట్": 8, "నైన్": 9, "టెన్": 10, "ఎలెవెన్": 11,
    "ట్వెల్వ్": 12,
}

# Any of the ways a clock time is MARKED, in either language. Used to tell an
# utterance that is about a time from one that merely contains a number.
CLOCK_MARK = re.compile(r"(o\s*.?\s*clock|గంట|ఓ\s*క్లాక్|बजे)", re.IGNORECASE)

# For a slot more than two days out. "%d/%m" was what this used, and a date read
# as digits down a phone line is not a day anybody writes down.
WEEKDAYS = ("సోమవారం", "మంగళవారం", "బుధవారం", "గురువారం",
            "శుక్రవారం", "శనివారం", "ఆదివారం")

# Money, not a clock. Run 266: the caller answered the BILL question with
# "మూడు లక్షలు" (three lakhs) and the number 3 was read as 3 oclock -- the agent
# announced "రేపు మధ్యాహ్నం three oclock కి బుక్ చేసుకున్నాం", stored
# 2026-08-29T15:00, and hung up 38 seconds into the call. The bill was recorded
# as 300000 and the appointment as 15:00: the same digit, twice.
#
# Any of these words means the number in the sentence is an amount. A time is
# never "lakhs".
MONEY = re.compile(
    r"(లక్ష|లచ్చ|వేల|వెయ్యి|కోటి|రూపాయ|రుపీ|బిల్లు|lakh|lac|crore|thousand|"
    r"rupee|rupees|rs\.?|₹|यूनिट|यूनिट्स|units?)", re.IGNORECASE)

# Every scale word the money parser knows, as whole tokens. MONEY above is a
# substring regex and misses the transliterated forms -- "ఫైవ్ లాక్స్" has no
# లక్ష in it. That went unnoticed for as long as it did because "ఫైవ్" was not a
# number word either, so the amount failed to parse as a TIME for the wrong
# reason. Adding the transliterated numerals took that accident away and left
# the real gap exposed: five lakhs a month became five o'clock.
#
# Read from `amounts.SCALES` rather than retyped, so the two cannot drift.
MONEY_TOKENS = frozenset(k for k in _SCALES if len(k) > 2) | {
    "రూపాయలు", "రూపాయల", "రుపీస్", "బిల్లు", "rupees", "rupee"}

# Telugu written in Unicode cannot be split on `\b`: vowel signs are combining
# marks, which `\w` does not count as word characters, so a boundary lands in
# the middle of a syllable. Splitting on SCRIPT RUNS instead is what
# `amounts.py` does, for the same reason and after the same bug.
#
# It matters here more than anywhere. "టు" (English "two") is two characters,
# and a substring search for it matches inside "ఉంటుంది", "కుదురుతుంది" and most
# other common verb endings -- so "ఆ బాగుంటుంది, ఓకే" ("yes, that's fine")
# parsed as two o'clock. Consent became a booking, which is precisely the
# failure this module was written to prevent.
_TOKEN = re.compile(r"[\d]+|[ఀ-౿]+|[a-zA-Z]+")

# Asking for the appointment to be MOVED, as opposed to mentioning a day.
#
# "ఎల్లుండి మా వాళ్ళు ఊరికి వెళ్తున్నారు" -- day after tomorrow my family are
# going to the village -- names a day and asks for nothing. Moving a confirmed
# visit on the strength of it is the same failure as reading "మూడు లక్షలు" as
# three o'clock: a value that happened to appear in the sentence, taken as an
# instruction nobody gave.
#
# So a statement moves a booking only when it also asks to. A question does not
# need this -- a question is never allowed to rebook at all, only to reopen the
# offer.
RESCHEDULE = re.compile(
    r"(మార్చ|చేయండి|చేయగలరా|పెట్టండి|పెట్టుకోండి|బదులు|కుదరదు|కాకుండా|"
    r"instead|change|reschedule|shift|move it|make it)", re.IGNORECASE)

# Choosing a menu item by its position. Two items only, so there is nothing
# between "first" and "second" to get wrong.
ORDINAL_FIRST = re.compile(
    r"(మొదటి|మొదలు|ఫస్ట్|first|one\s*st|1\s*st)", re.IGNORECASE)
ORDINAL_SECOND = re.compile(
    r"(రెండో|రెండవ|సెకండ్|second|2\s*nd)", re.IGNORECASE)

# Consent to meet. NOT a time -- see the module docstring.
AGREEMENT = re.compile(
    r"(సరే|అలాగే|ఓకే|ok(ay)?|బాగుంటుంది|బాగుంది|కుదురుతుంది|పర్వాలేదు|"
    r"చేద్దాం|పెట్టుకోండి|అవును|యస్|sure|fine|works)", re.IGNORECASE)

REFUSAL = re.compile(
    r"(వద్దు|అవసరం\s*లేదు|కుదరదు|కుదరద|ఇష్టం\s*లేదు|no\s*need|not\s*interested|"
    r"వేరే\s*సమయం|తర్వాత\s*చెప్తా)", re.IGNORECASE)


@dataclass(frozen=True)
class Slot:
    """One offerable appointment time."""

    when: datetime

    @property
    def iso(self) -> str:
        return self.when.isoformat()

    def say(self, now: datetime | None = None) -> str:
        """How the agent reads it aloud.

        The hour is spoken in English because that is what these callers use for
        numbers, while the day and part-of-day stay Telugu -- which is exactly
        how run 262's caller and agent both spoke.
        """
        today = (now or datetime.now(IST)).astimezone(IST).date()
        # A slot further out than "ఎల్లుండి" is named by its weekday, not by its
        # date. This used to fall back to "%d/%m", so a slot ten days out was
        # read to the caller as "01/09" -- and a date spoken as digits is not
        # something anybody writes down, which is the whole job of this line.
        day = {0: "ఈ రోజు", 1: "రేపు", 2: "ఎల్లుండి"}.get(
            (self.when.date() - today).days) or WEEKDAYS[self.when.weekday()]
        part = ("ఉదయం" if self.when.hour < 12
                else "మధ్యాహ్నం" if self.when.hour < 16 else "సాయంత్రం")
        hour12 = self.when.hour if self.when.hour <= 12 else self.when.hour - 12
        names = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
                 7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
                 12: "twelve"}
        # "ten o'clock" -- a CLOCK TIME, in English, both halves.
        #
        # A clock time is English on both halves; a DURATION is Telugu.
        #
        #     five o'clock   when the vendor arrives
        #     five గంటలు      how many hours of sunlight the roof gets
        #
        # The client's two corrections read as contradictory -- "9 to 10 గంటలు"
        # and then "5 o'clock, not 5 గంటలు" -- until you notice they are about
        # different things. Both are right. A pass on 29 Aug rewrote every
        # o'clock to గంటలకు, which fixed the duration and broke the clock.
        #
        # The parser reads either form back: it accepts "గంట" AND "o clock" as
        # clock markers and resolves "ten" through NUMBER_WORDS.
        return f"{day} {part} {names.get(hour12, str(hour12))} o'clock"


def _as_dt(value) -> datetime | None:
    """Accept either an ISO string or a datetime; anything else is not a slot."""
    if isinstance(value, datetime):
        return value.astimezone(IST)
    try:
        return datetime.fromisoformat(str(value)).astimezone(IST)
    except (TypeError, ValueError):
        return None


def _clamp(when: datetime) -> datetime:
    """Push a time into daylight business hours, moving to the next day if needed."""
    when = when.replace(minute=0, second=0, microsecond=0)
    if when.hour < FIRST_HOUR:
        return when.replace(hour=FIRST_HOUR)
    if when.hour >= LAST_HOUR:
        return (when + timedelta(days=1)).replace(hour=FIRST_HOUR)
    return when


def offer_slots(now: datetime | None = None,
                taken: Iterable[str | datetime] = ()) -> tuple[Slot, Slot]:
    """Two concrete, distinguishable options that are not already booked.

    Deliberately on DIFFERENT days as well as different times. Two slots on one
    day are easy to confuse on a noisy phone line, and a confused caller is the
    booking that goes wrong.

    `taken` is every appointment already promised to somebody else. Offering one
    of those is the worst failure this module can produce: two customers are
    each told a vendor is coming, both wait in, and one of them is stood up. It
    costs the client the customer, not just the visit.

    The search walks forward day by day rather than shuffling times within a
    day, so the two offers stay far apart and stay easy to tell apart on a bad
    line even after several slots are gone.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    busy = {_as_dt(t) for t in taken}
    busy.discard(None)

    free = [
        when
        for day in range(1, MAX_DAYS_AHEAD + 1)
        for hour in OFFER_HOURS
        if (when := _clamp((now + timedelta(days=day)).replace(hour=hour))) > now
        and when not in busy
    ]
    free.sort()

    chosen: list[datetime] = []
    if free:
        chosen.append(free[0])
        # The second offer must differ in BOTH the day and the hour. Two slots
        # that differ only by the day word -- "రేపు ఉదయం ten" against "ఎల్లుండి
        # ఉదయం ten" -- are one mishearing apart on a phone line, and a misheard
        # slot is a vendor at the door on the wrong morning.
        for when in free[1:]:
            if when.date() != chosen[0].date() and when.hour != chosen[0].hour:
                chosen.append(when)
                break
        else:
            # Nothing differs on both axes; a different day alone still beats
            # offering the same slot twice.
            for when in free[1:]:
                if when.date() != chosen[0].date():
                    chosen.append(when)
                    break
    if len(chosen) == 2:
        return Slot(chosen[0]), Slot(chosen[1])

    # Everything inside the horizon is spoken for. Offer the far end rather than
    # returning nothing: a human can move a booking, but the agent cannot
    # improvise a time if it is handed none.
    fallback = _clamp((now + timedelta(days=MAX_DAYS_AHEAD)).replace(hour=OFFER_HOURS[0]))
    while len(chosen) < 2:
        chosen.append(fallback)
        fallback = _clamp(fallback + timedelta(days=1))
    return Slot(chosen[0]), Slot(chosen[1])


def is_taken(when: datetime, taken: Iterable[str | datetime]) -> bool:
    """Whether that exact slot is already promised to somebody else."""
    target = _as_dt(when)
    return any(_as_dt(t) == target for t in taken)


def _named_hour(low: str, tokens: list[str]) -> int | None:
    """The clock hour said out loud, 1-24, before any part-of-day adjustment.

    Pulled out of `parse_slot` so `names_a_time_unprompted` can ask the same
    question without duplicating the answer -- the two drifting apart is how a
    caller's time gets accepted by one and dropped by the other.
    """
    m = re.search(r"\b(\d{1,2})\s*(?::\s*\d{2})?\s*(o\s*.?\s*clock|గంట|बजे)?", low)
    if m and 1 <= int(m.group(1)) <= 24:
        return int(m.group(1))
    # Whole tokens only. See _TOKEN: a substring search for "టు" matches inside
    # half the verbs in the language.
    for tok in tokens:
        if tok in NUMBER_WORDS:
            return NUMBER_WORDS[tok]
    return None


def _is_money(text: str, low: str, tokens: list[str]) -> bool:
    return bool(MONEY.search(text)) or any(tok in MONEY_TOKENS for tok in tokens)


def names_a_time_unprompted(text: str) -> bool:
    """Is this unmistakably an appointment, with no menu behind it?

    `CallState.note_booking` refuses to read any number as a time until two
    slots have been put to the caller. That gate is right -- run 266 booked a
    site visit out of "మూడు లక్షలు", which was an answer to the BILL question on
    turn three -- and it is too wide.

    Run 323. The agent asked, in as many words, "what time suits you?", and got:

        USER   ఎల్లుండి సాయంత్రం ఐదు ఇంటికి

    Day after tomorrow, evening, five, at my house. No menu had been rendered
    yet, so `offered` was empty and the whole utterance was discarded. Two turns
    later the agent told him "మీరు చెప్పిన సమయం మా ఎంపికలలో లేదు" -- the time you
    said is not among our options -- and booked him for TODAY at four. Two days
    and one hour out, agreed by both parties, and visible to neither.

    So the test is the utterance's STRUCTURE, not whether permission was
    granted first. A bill has no day word in it. "మూడు లక్షలు" has no ఎల్లుండి,
    no రేపు, no ఈ రోజు, and it never will.

    A day word ALONE is not enough either, and that is the other half of this.
    "ఎల్లుండి మా వాళ్ళు ఊరికి వెళ్తున్నారు" -- day after tomorrow my family are
    going to the village -- names a day and asks for nothing, and booking a
    visit on the strength of it is run 266's bug wearing a different hat. An
    hour has to be named too.
    """
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()
    tokens = _TOKEN.findall(low)
    if _is_money(t, low, tokens):
        return False
    if not any(w in low or w in t for w in DAY_WORDS):
        return False
    return _named_hour(low, tokens) is not None


def parse_slot(text: str, now: datetime | None = None,
               offered: Iterable["Slot"] = ()) -> datetime | None:
    """The specific time the caller named, or None if they did not name one.

    None is the important return value. It means "they have not chosen", which
    must lead to one more question -- never to picking a slot on their behalf.

    `offered` is the menu currently on the table, and it settles the DAY when
    the caller answers with the hour alone. Run 323 is what happens without it:

        AGENT  రేపు ఉదయం ten o'clock లేదా ఎల్లుండి సాయంత్రం four o'clock?
        CALLER 4 ఓ క్లాక్
        AGENT  సరే, ఈ రోజు సాయంత్రం four o'clockకి ...

    He picked the second option. There was exactly one four in the menu and it
    was two days away; the parser had never been shown the menu, so it resolved
    a bare "four" against the wall clock and booked TODAY. The stored record
    reads 2026-08-30T16:00 against an offer of 2026-09-01T16:00.

    A caller choosing from a menu says the part that distinguishes the options
    and drops the rest. That is not sloppiness -- it is how anyone answers a
    closed question -- so the dropped half has to come from the menu.
    """
    t = (text or "").strip()
    if not t:
        return None
    now = (now or datetime.now(IST)).astimezone(IST)
    low = t.lower()
    tokens = _TOKEN.findall(low)
    if _is_money(t, low, tokens):
        # An amount, not an appointment. Reading one as the other books a visit
        # the caller never agreed to and ends the call -- run 266.
        return None

    slots = [s for s in offered if isinstance(getattr(s, "when", None), datetime)]

    day_offset = None
    for word, offset in DAY_WORDS.items():
        if word in low or word in t:
            day_offset = offset
            break

    part_hour = None
    for word, h in PART_OF_DAY.items():
        if word in t or word in low:
            part_hour = h
            break
    hour = part_hour

    # "the first one" / "the second one". A caller who answers a two-item menu
    # by position has chosen just as definitely as one who reads the time back,
    # and run 262's whole lesson is that a choice must never be guessed at.
    #
    # Checked BEFORE the hour, because "ఫస్ట్ ఒకటి" -- the first one -- contains
    # a numeral that is not a time. An explicit ordinal is what the caller meant;
    # a number sitting next to it is the English word "one", not one o'clock.
    if day_offset is None and len(slots) == 2:
        if ORDINAL_FIRST.search(t):
            return slots[0].when
        if ORDINAL_SECOND.search(t):
            return slots[1].when

    # An explicit clock time wins over the part of day: "ఉదయం ten oclock" is
    # ten, not the generic morning slot.
    named = _named_hour(low, tokens)
    if named is not None:
        # The menu decides the day when the caller names only the hour. Done
        # BEFORE the +12 adjustment, because the caller said "four" and the slot
        # holds 16:00 -- they are the same time and only one of them is written
        # the way it was spoken.
        if day_offset is None and slots:
            matches = [s for s in slots
                       if s.when.hour == named or s.when.hour % 12 == named % 12]
            if len(matches) == 1:
                return matches[0].when
        if named <= 12 and part_hour is not None and part_hour >= 12:
            named += 12                      # "మధ్యాహ్నం two" -> 14:00
        elif named <= 7 and part_hour is None:
            named += 12                      # a bare "five" about a site visit
        hour = named

    if day_offset is None and hour is None:
        return None
    if day_offset is None:
        # A time with no day means the next occurrence of it.
        day_offset = 0 if (hour or 0) > now.hour else 1
    if hour is None:
        hour = 10                            # a day with no time: mid-morning

    when = (now + timedelta(days=day_offset)).replace(
        hour=min(max(hour, 0), 23), minute=0, second=0, microsecond=0)
    if when <= now:
        when += timedelta(days=1)
    return _clamp(when)


def agreed(text: str) -> bool:
    """They are willing to meet. Says nothing about WHEN."""
    t = text or ""
    return bool(AGREEMENT.search(t)) and not REFUSAL.search(t)


def declined(text: str) -> bool:
    return bool(REFUSAL.search(text or ""))
