"""Synchronous hard-stop detection on the caller's utterance.

The extractor is deliberately asynchronous -- it must never delay a reply, so
its result only lands in the state block for the NEXT turn. That one-turn lag is
invisible for ordinary facts.

It is NOT acceptable for hard stops. The 30-persona run showed why: on the turn
where the caller says "I already have solar" or "this is a fraud", the agent had
already produced a pitch before the extractor caught up. Eight compliance
violations came from that single lag.

So hard stops are detected here instead: deterministic patterns, microseconds,
run BEFORE the reply is generated. Zero latency cost.

Precision over recall, deliberately. A false positive ends a good call, which is
far worse than a one-turn lag on a soft signal. Patterns here must be ones that
essentially cannot appear in a normal cooperative conversation -- softer signals
are left to the extractor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from loguru import logger

# --- remove me / stop calling ------------------------------------------------
REMOVAL = re.compile(
    r"(లిస్ట్\s*(నుంచి|నుండి)?\s*తీసే|నంబర్\s*తీసే|కాల్\s*చేయ(కండి|వద్దు|కు)"
    r"|ఇంక\s*కాల్\s*చేయ|మళ్ళీ\s*కాల్\s*చేయ(కండి|వద్దు)|డిస్టర్బ్\s*చేయ(కండి|వద్దు)"
    r"|हटा\s*दीजिए|कॉल\s*मत\s*कर|dnd"
    r"|remove\s+(me|my\s+number)|do\s*n[o']?t\s+call|stop\s+calling|unsubscribe)",
    re.IGNORECASE)

# --- fraud / scam accusation -------------------------------------------------
FRAUD = re.compile(
    r"(మోసం|ఫ్రాడ్|చీటింగ్|దొంగ|నమ్మ(లేను|కం\s*లేదు)"
    r"|धोखा|फ्रॉड|ठग"
    r"|fraud|scam|cheat(ing)?|fake\s+call)",
    re.IGNORECASE)

# --- wrong number / wrong person --------------------------------------------
WRONG_NUMBER = re.compile(
    r"(రాంగ్\s*నంబర్|తప్పు\s*నంబర్|ఆ\s*పేరు.{0,12}(తెలియదు|లేదు)"
    r"|అలాంటి\s*వాళ్ళు\s*(ఎవరూ\s*)?లేరు|ఇది\s*నా\s*నంబర్\s*కాదు"
    r"|गलत\s*नंबर|wrong\s+number|no\s+one\s+by\s+that\s+name)",
    re.IGNORECASE)

# --- already has the product -------------------------------------------------
ALREADY_HAS = re.compile(
    r"((ఇప్పటికే|అల్రెడీ).{0,25}(సోలార్|ప్యానెల్|పెట్టుకున్న|ఉంది|వేయించుకున్న)"
    r"|సోలార్.{0,15}(ఇప్పటికే|అల్రెడీ|పెట్టుకున్నాను|ఉంది|వేయించుకున్నాను)"
    r"|पहले\s*से.{0,20}(सोलर|लगा)"
    r"|already\s+(have|got|installed)\s+(solar|panels))",
    re.IGNORECASE)

# --- "I already told you" ----------------------------------------------------
# The caller saying they have already answered is the clearest possible signal
# that the checklist is wrong, and it should be believed instantly rather than
# after the extractor catches up. Run 218: "చెప్పాను కదా అప్పుడే",
# "అదే 10 టు 20 లాక్స్ చెప్పాను కదా", then "ఎన్ని సార్లు అడుగుతారు?" and the
# call ended. Asking a third time after this is not persistence, it is not
# listening.
ALREADY_ANSWERED = re.compile(
    r"(చెప్పాను\s*కదా|చెప్పాన్నే|అప్పుడే\s*చెప్ప|ఇంతకుముందే\s*చెప్ప|"
    r"మళ్ళీ\s*ఎందుకు|ఎన్ని\s*సార్లు\s*అడు|అదే\s*చెప్|చెప్తున్నా\s*కదా"
    r"|पहले\s*ही\s*बता|कितनी\s*बार"
    r"|already\s+(told|said|answered)|i\s+said\s+that|how\s+many\s+times)",
    re.IGNORECASE)


# --- a child answered --------------------------------------------------------
CHILD = re.compile(
    r"((అమ్మ|నాన్న|డాడీ|మమ్మీ).{0,20}(లేరు|బయటికి|ఇంట్లో\s*లేరు)"
    r"|నేను.{0,10}(చిన్న|పిల్ల)"
    r"|(मम्मी|पापा).{0,15}(नहीं|बाहर)"
    r"|mummy|papa\s+(is\s+)?not\s+(at\s+)?home)",
    re.IGNORECASE)


# --- the caller is ready to book --------------------------------------------
# A buying signal outranks the checklist (Layer 2, "Reading the call"). Layer 2
# says so in prose and the model still ignored it, because STILL_NEED sits at
# the very end of the context and recency wins. So it is detected here instead
# and the checklist is physically removed for that turn.
BUYING = re.compile(
    r"(వచ్చి\s*చూడ|వచ్చేయ|ఒకసారి\s*రండి|ఎప్పుడు\s*వస్తారు|ఎంత\s*(అవుతుంది|ఖర్చు)"
    r"|ఎలా\s*పని\s*చేస్తుంది|వారంటీ|బుక్\s*చే|ఇన్‌స్టాల్\s*ఎప్పుడు"
    r"|आकर\s*देख|कब\s*आओगे|कितना\s*(लगेगा|खर्च)"
    r"|come\s+(and\s+)?(see|have\s+a\s+look|visit)|how\s+much\s+(does|will)"
    r"|book\s+(it|the|a)\s*(visit|slot)?)",
    re.IGNORECASE)

# --- a time has been accepted ------------------------------------------------
# Deliberately narrow: a bare "సరే" is far too common to treat as a booking.
# We require a time word, or an explicit "come".
AGREED = re.compile(
    r"((రేపు|ఎల్లుండి|పొద్దున|సాయంత్రం|మధ్యాహ్నం|ఆదివారం|శనివారం)"
    r".{0,25}(కుదురుతుంది|ఓకే|సరే|వచ్చేయండి|రండి|పర్వాలేదు)"
    r"|(కుదురుతుంది|ఓకే|సరే).{0,15}(రేపు|పొద్దున|సాయంత్రం|మధ్యాహ్నం)"
    r"|(कल|सुबह|शाम).{0,20}(ठीक|ओके|आ\s*जाओ)"
    r"|(tomorrow|morning|evening|sunday|saturday).{0,20}(is\s+)?(fine|ok|okay|works|good))",
    re.IGNORECASE)

# --- stop interrogating me ---------------------------------------------------
NO_MORE_QUESTIONS = re.compile(
    r"(ప్రశ్నలు\s*(వద్దు|ఆపండి)|ఇంకేమీ\s*అడగ(కండి|వద్దు)|అడగడం\s*ఆపండి"
    r"|సవాలక్ష\s*ప్రశ్నలు|ఇన్ని\s*ప్రశ్నలు"
    r"|सवाल\s*मत\s*पूछ|और\s*सवाल\s*नहीं"
    r"|(stop|no\s+more)\s+questions?|don'?t\s+ask\s+(me\s+)?(any)?\s*more)",
    re.IGNORECASE)

# --- a plain refusal ---------------------------------------------------------
# One refusal earns exactly one gentle probe (Layer 2). The SECOND one ends the
# call. Counting happens in `apply` because it needs the call's history --
# `not_interested` scored 2.81/10 purely because the agent asked again.
REFUSAL = re.compile(
    r"(ఆసక్తి\s*లేదు|ఇష్టం\s*లేదు|అవసరం\s*లేదు|వద్దు\s*సార్|వద్దండి|అక్కర్లేదు"
    r"|నాకు\s*వద్దు|చెప్పాను\s*కదా\s*వద్దు"
    r"|कोई\s*दिलचस्पी\s*नहीं|ज़रूरत\s*नहीं|नहीं\s*चाहिए"
    r"|not\s+interested|no\s+need|don'?t\s+want)",
    re.IGNORECASE)


@dataclass
class Triage:
    must_end: bool = False
    reason: str = ""
    disqualified: bool = False
    disqualify_reason: str = ""
    buying_signal: bool = False
    next_step_agreed: bool = False
    no_more_questions: bool = False
    already_answered: bool = False

    @property
    def any(self) -> bool:
        return (self.must_end or self.disqualified or self.buying_signal
                or self.next_step_agreed or self.no_more_questions)


def triage(text: str) -> Triage:
    """Classify a caller utterance for hard stops. Pure, fast, no model call."""
    t = (text or "").strip()
    if not t:
        return Triage()

    if REMOVAL.search(t):
        return Triage(must_end=True,
                      reason="The caller asked to be removed from the list.")
    if FRAUD.search(t):
        return Triage(must_end=True,
                      reason="The caller believes this is a fraud. Do not ask "
                             "for any detail and do not defend the company.")
    if WRONG_NUMBER.search(t):
        return Triage(must_end=True,
                      reason="Wrong number or wrong person. Apologise briefly.")
    if CHILD.search(t):
        return Triage(must_end=True,
                      reason="A child answered. Do not sell and do not ask them "
                             "anything. Politely say you will call back later.")
    if ALREADY_HAS.search(t):
        return Triage(disqualified=True,
                      disqualify_reason="already has solar installed")

    # Closing signals. These do not end the call by themselves -- they change
    # what the agent is allowed to do on THIS turn.
    return Triage(
        next_step_agreed=bool(AGREED.search(t)),
        buying_signal=bool(BUYING.search(t)),
        no_more_questions=bool(NO_MORE_QUESTIONS.search(t)),
        already_answered=bool(ALREADY_ANSWERED.search(t)),
    )


def apply(state, text: str) -> Triage:
    """Run triage and latch the result into CallState before the reply.

    Everything here is synchronous and deterministic, and runs BEFORE the reply
    is generated. That is the whole point: the async extractor lands one turn
    late, and one turn late is exactly when the damage is done -- the agent gets
    one more question in after the caller has already said stop, or booked.
    """
    result = triage(text)
    if result.must_end:
        state.must_end = True
        state.end_reason = result.reason
    if result.disqualified:
        state.disqualified = True
        state.disqualify_reason = result.disqualify_reason

    # A second refusal ends the call. The first one buys a single gentle probe.
    if REFUSAL.search((text or "").strip()):
        state.refusals += 1
        if state.refusals >= 2:
            state.must_end = True
            state.end_reason = ("The caller has now refused twice. Do not probe "
                                "again. Thank them warmly and end the call.")
            result.must_end = True
            result.reason = state.end_reason

    # Latch, never un-set -- a caller who agreed to a visit has agreed, even if
    # they chat about something else on the next turn.
    if result.already_answered:
        # Believe them at once. The field they are being re-asked is the one
        # currently at the head of the checklist, so exhausting its budget stops
        # it being asked a third time -- which is what ended run 218.
        field_name = state.pending_ask or (
            state.still_need[0] if state.still_need else "")
        if field_name:
            state.ask_counts[field_name] = state.MAX_ASKS_PER_FIELD
            state.pending_ask = ""
            logger.info(f"triage: caller says they already answered "
                        f"{field_name!r}; moving on")

    # A named time books the visit. Bare consent deliberately does not: see
    # CallState.note_booking and run 262.
    if hasattr(state, "note_booking") and state.note_booking(text):
        logger.info(f"triage: appointment set for {state.appointment_iso}")

    if result.next_step_agreed:
        state.next_step_agreed = True
    if result.buying_signal:
        state.buying_signal = True
    if result.no_more_questions:
        state.no_more_questions = True
    return result
