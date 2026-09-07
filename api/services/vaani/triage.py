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

from api.services.vaani.completeness import sounds_unfinished

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


# --- "I have NOT told you yet" ----------------------------------------------
# The exact opposite of ALREADY_ANSWERED, and it had no pattern at all -- which
# is how run 804 lost three of its six fields.
#
# The agent asked for the bill, cut the caller off mid-answer ("వచ్చేసి"), and
# moved on. He then said, three times, in plain Telugu, that he had not answered:
#
#     "ఏం బిల్ చెప్పలా ఏదో"                     I didn't say the bill
#     "నేను చెప్పలే కదా ఎందుకు ముందుకు పోతున్నావ్"  I didn't say it -- why move on?
#     "బిల్లు తెలుసుకోలేదు కదా, మరి నెక్స్ట్ క్వశ్చన్ ఏమి పోయినారు?"
#
# None of the three was recognised. `monthly_bill` hit its two-ask cap and left
# the checklist for good, so the one thing he was asking to be asked was the one
# thing the agent could no longer ask. Saved as null.
#
# A caller saying "I have not answered" is the same class of signal as
# ALREADY_ANSWERED -- the caller telling us the checklist is wrong -- and it is
# believed the same way, instantly, rather than waiting for the extractor.
#
# The two patterns are opposites and cannot both fire: "చెప్పాను" (I said) has
# no negative suffix, and every alternative here requires one.
NOT_YET_ANSWERED = re.compile(
    r"(చెప్ప(లేదు|లేద|లే|లా)|తెలుసుకో(లేదు|లేద)|అడగ(లేదు|లేద)"
    r"|ఆన్సర్\s*(చేయ|ఇవ్వ)(లేదు|లేద|లేక)|ఇంకా\s*చెప్ప|చెప్పనే\s*లేదు"
    r"|(ముందుకు|నెక్స్ట్).{0,18}(పోతున్|పోయిన|వెళ్ళ|వెళ్త)"
    r"|नहीं\s*बताया|अभी\s*तक\s*नहीं"
    r"|did\s*n[o']?t\s+(tell|say|answer)|haven[o']?t\s+(told|said|answered)"
    r"|you\s+skipped|why.{0,30}next\s+question)",
    re.IGNORECASE)


# --- "I cannot tell you that" -------------------------------------------------
# The exact opposite of NOT_YET_ANSWERED, and one character away from it.
#
#     చెప్పలేదు   past negative    "I did NOT say it"    -> you skipped me, ask again
#     చెప్పలేను   ability negative "I CANNOT say it"     -> a refusal, move on
#
# `లేను` sat inside the NOT_YET_ANSWERED alternation, so a caller declining a
# question was read as a caller complaining he had been skipped -- and the
# handler for that is `_refund_ask`, which puts the field BACK into
# `still_need` and gives back the ask that was spent on it. A refusal therefore
# reset the two-ask budget instead of ending it, and the agent could ask the
# same thing without limit. Run 844: "నేను చెప్పలేను" and the bill was asked
# four times, twice after he had declined it.
#
# The client's description is exact: "if customer don't want to answer, skip
# the answer". This is the signal that lets that happen.
CANNOT_ANSWER = re.compile(
    r"(చెప్పలేను|చెప్పలేము|చెప్పను|చెప్పదల్చుకోలేదు"
    r"|తెలియదు|తెలీదు|గుర్తు\s*లేదు|గుర్తుకు\s*రావట్లేదు"
    r"|ఇవ్వను|ఇవ్వలేను|అవసరం\s*లేదు"
    r"|नहीं\s*बताऊंगा|मुझे\s*नहीं\s*पता"
    # `can\s*n[o']?t` cannot match "can't": the apostrophe stands WHERE the n
    # would be, so the alternatives are spelled out rather than assembled.
    r"|(i\s+)?(can[o'’n]*t|cannot|can\s+not|won[o'’]?t"
    r"|do\s*n[o'’]?t\s+want\s+to)\s+(say|tell|share|give)"
    r"|do\s*n[o']?t\s+know|no\s+idea|not\s+sure)",
    re.IGNORECASE)


# --- the caller is saying goodbye -------------------------------------------
# Run 803 ended with the agent repeating one booking confirmation SEVEN times.
# The caller said goodbye four times and finally asked "బాయ్ బాయ్ కట్ చేస్తారా
# మీరు" -- will you hang up? -- because the agent would not.
#
# `must_end` was reachable only from a removal, a fraud accusation, a second
# refusal, or the model emitting `MODE: END` of its own accord. It never did.
# So a caller who has finished the call has no way to end it, and the state
# block hands the model the same closing instruction every turn forever.
#
# Deliberately NOT matching "ఉంటాను". It is the polite Telugu sign-off AND the
# ordinary verb for "I live" -- "హైదరాబాద్‌లో ఉంటాను" is run 803's location
# answer, and hanging up on it would lose the call it is trying to save.
FAREWELL = re.compile(
    r"((^|\s)(బాయ్|బై|టాటా)(\s|$|\.|,)|వీడ్కోలు|శెలవు"
    r"|(కాల్|ఫోన్)\s*(కట్|పెట్టే)|కట్\s*చేస్(తారా|తా|ేయండి)"
    r"|अलविदा|फ़ोन\s*रख"
    r"|(^|\s)bye(\s|$|\.|,|-)|good\s*bye|hang\s*up|cut\s+the\s+call)",
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

# --- the caller is claiming the floor ----------------------------------------
#
# Run 324, and this is the one the client is angriest about. The whole call:
#
#     38.20  BOT   సరే, మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?
#     40.58  USER  నేను చెప్తే వినండి.                 listen to what I say
#     43.02  BOT   సరే, మీరు ఏ ఏరియా లేదా సిటీలో ఉంటారు?   (asks it again)
#     45.68  USER  నేను చెప్పేది మాకు                  (cut off mid-sentence)
#     49.48  BOT   మంచిది సార్, మీది సొంత ఇల్లా ...      (moves on regardless)
#
# He says LISTEN TO ME, twice, in as many words, and is asked the same question
# and then a different one. `location` was saved as null. His own words earlier
# in the same call: "చెప్పండి వినండి మీరు" -- speak, YOU listen.
#
# This is not a refusal and it is not a deferral. Both of those mean stop; this
# one means WAIT, I have not finished. Treating it as a refusal would end the
# call on a caller who is trying to buy, and treating it as nothing is what
# produced the transcript above.
#
# Two consequences, and the second matters more than the first:
#   - ask nothing this turn, so he gets the floor he asked for
#   - do not SPEND the ask, so the question he was interrupted answering is
#     still on the checklist and gets asked again later
WANTS_THE_FLOOR = re.compile(
    r"(నేను\s*చెప్తే|నేను\s*చెప్పేది|నేను\s*చెప్పింది|చెప్పనివ్వండి"
    r"|నేను\s*చెప్తున్నా|చెప్తున్నా\s*కదా|వినండి\s*మీరు|మీరు\s*వినండి"
    r"|కొంచెం\s*వినండి|ఆగండి|ఒక్క\s*నిమిషం\s*ఆగ|పూర్తిగా\s*వినండి"
    r"|मेरी\s*बात\s*सुन|सुनिए\s*पहले"
    r"|let\s+me\s+(finish|speak|talk|tell)|listen\s+to\s+me"
    r"|hear\s+me\s+out|hold\s+on|wait\s+a\s+(minute|second))",
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


# --- "we'll think about it and let you know" ---------------------------------
# A deferral is NOT a refusal. He has not said no; he has said not now. The
# difference matters, because the two deserve opposite handling and run 314 gave
# them the same one:
#
#     USER : ఆ చెప్తాం మేము మళ్ళీ చెప్తాం.          we'll tell you, we'll tell you again
#     BOT  : రేపు ఉదయం ten oclock లేదా ... ఏ సమయం మీకు బాగుంటుంది?
#     USER : మేము చెప్తాం ఆలోచన చెప్తాం.            we'll think and tell you
#     BOT  : రేపు ఉదయం ten oclock లేదా ... దయచేసి ... చెప్పండి.   <- the SAME question, plus "please"
#     USER : అంటే మేము ఆలోచించి చెప్తాం దాని గురించి డిసైడ్ అవ్వలేం ఇంకా
#            we'll think about it and tell you, we can't decide yet
#
# Three deferrals, and the agent asked its closing question twice near-verbatim,
# the second time pleading. That is the behaviour the client described as
# irritating, and he is right: pushing a "let me think" does not convert it, it
# only converts a warm lead into someone who will not take the next call.
#
# Unlike a refusal, ONE of these ends the close. There is no gentle second
# probe to be had -- the second ask IS the irritation.
DEFERRAL = re.compile(
    r"(ఆలోచించి\s*చెప్తా|ఆలోచించి\s*చెబుతా|ఆలోచిస్తా|ఆలోచించాలి"
    r"|మళ్ళీ\s*చెప్తా|మళ్లీ\s*చెప్తా|తర్వాత\s*చెప్తా|తరువాత\s*చెప్తా"
    r"|మేము\s*చెప్తాం|నేను\s*చెప్తాను|చెప్తాం\s*మేము"
    r"|డిసైడ్\s*అవ్వలే|డిసైడ్\s*చేసుకొని|నిర్ణయించుకొని"
    r"|ఇంకా\s*డిసైడ్|కొంచెం\s*టైమ్|టైమ్\s*కావాలి|తర్వాత\s*మాట్లాడ"
    r"|घर\s*में\s*बात|सोचकर\s*बताता|बाद\s*में\s*बता"
    r"|(think|thought)\s+(about|it|over)|let\s+you\s+know|get\s+back\s+to\s+you"
    r"|call\s+you\s+back|need\s+(some\s+)?time|not\s+decided|can'?t\s+decide"
    r"|later|maybe\s+later)",
    re.IGNORECASE)


@dataclass
class Triage:
    must_end: bool = False
    reason: str = ""
    disqualified: bool = False
    disqualify_reason: str = ""
    buying_signal: bool = False
    next_step_agreed: bool = False
    # The caller has asked for the floor. Not a stop -- a "wait".
    wants_the_floor: bool = False
    no_more_questions: bool = False
    already_answered: bool = False
    deferred: bool = False

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
    if FAREWELL.search(t):
        return Triage(must_end=True,
                      reason="The caller is saying goodbye. Say one short "
                             "farewell and END THE CALL. Do not re-state the "
                             "appointment, do not ask anything, do not pitch.")
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
    # An accepted slot outranks a deferral: "రేపు ఉదయం ఓకే, తర్వాత చెప్తాను"
    # is a booking with a comment attached, not a postponement.
    agreed = bool(AGREED.search(t))
    return Triage(
        next_step_agreed=agreed,
        buying_signal=bool(BUYING.search(t)),
        no_more_questions=bool(NO_MORE_QUESTIONS.search(t)),
        wants_the_floor=bool(WANTS_THE_FLOOR.search(t)),
        already_answered=bool(ALREADY_ANSWERED.search(t)),
        deferred=bool(DEFERRAL.search(t)) and not agreed,
    )


# A field may be given back at most this many times.
#
# Refunds exist so an interrupted caller keeps his question. They must not
# become an unbounded budget, and the ceiling is not a matter of taste: run 218
# hung up after being asked the same thing FOUR times. One refund on top of the
# two-ask cap makes three the most a caller can ever be asked, which stays under
# that. Replaying run 804 with a cap of two produced four consecutive bill
# questions -- the fix rebuilding the bug it was written to fix.
MAX_REFUNDS_PER_FIELD = 1


def _abandon_ask(state, why: str) -> None:
    """Spend the field's whole budget, so it leaves `still_need` for good.

    The mirror of `_refund_ask`. A caller who has declined a question does not
    need it asked again in different words -- he needs it dropped. Pushing the
    count to the cap is how a field leaves the checklist, and it is the same
    door the two-ask budget already uses, so nothing downstream has to learn a
    new state.
    """
    # Falls back to the HEAD OF THE CHECKLIST, like the `already_answered`
    # path above. `pending_ask` is cleared as soon as a reply is produced and
    # `last_asked` with it, so by the time the caller's refusal is triaged both
    # are routinely empty -- and without this fallback the function returned
    # having done nothing, silently. Measured on run 845: the caller said
    # "నేను చెప్పలేను" and was asked the bill twice more.
    still = getattr(state, "still_need", None) or []
    field_name = (getattr(state, "pending_ask", "")
                  or getattr(state, "last_asked", "")
                  or (still[0] if still else ""))
    counts = getattr(state, "ask_counts", None)
    if not field_name or counts is None:
        return
    cap = getattr(state, "MAX_ASKS_PER_FIELD", 2)
    if counts.get(field_name, 0) >= cap:
        return
    counts[field_name] = cap
    state.last_asked = ""
    state.pending_ask = ""
    logger.info(f"triage: abandoning {field_name!r} -- {why}")


def _refund_ask(state, why: str) -> None:
    """Give back the ask that was spent on a turn the caller never completed."""
    field_name = (getattr(state, "pending_ask", "")
                  or getattr(state, "last_asked", "") or "")
    counts = getattr(state, "ask_counts", None)
    if not field_name or counts is None:
        return
    refunds = getattr(state, "refunds", None)
    if refunds is not None:
        if refunds.get(field_name, 0) >= MAX_REFUNDS_PER_FIELD:
            logger.info(f"triage: NOT refunding {field_name!r} again -- "
                        f"already given back {MAX_REFUNDS_PER_FIELD} times")
            return
        refunds[field_name] = refunds.get(field_name, 0) + 1
    if counts.get(field_name, 0) > 0:
        counts[field_name] -= 1
        # Cleared so a caller who is cut off twice on the same question does
        # not earn the field an unbounded budget -- which would be run 218
        # rebuilt out of refunds.
        state.last_asked = ""
        logger.info(f"triage: refunding the ask on {field_name!r} -- {why}")


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

    # A deferral, once the close has actually been put to him, ends the call.
    #
    # Not counted like a refusal, because there is no useful second ask. Run 314
    # made the second ask -- the same sentence with "దయచేసి" (please) bolted on
    # -- and got a third, firmer deferral for it. The lead was warm up to that
    # point: he had given his bill, his roof, his name, and asked two questions
    # about the company. He was pushed until he stopped being warm.
    #
    # Gated on `offered`, so the rule only applies once slots have been named.
    # "I'll tell you the bill later" earlier in the call is a deferral about a
    # question, not about the appointment, and it must not hang up on him.
    if result.deferred and getattr(state, "offered", ()):
        state.must_end = True
        state.end_reason = (
            "The caller wants time to think. Do NOT ask about the appointment "
            "again and do NOT offer another time. Thank them warmly by name, "
            "say they can call whenever they are ready, and end the call.")
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

    # Money, read deterministically the moment it is spoken.
    if hasattr(state, "note_amount") and state.note_amount(text):
        logger.info(f"triage: bill = {state.amount.rupees:,} ({state.amount.say()})")

    # A day ruled out is never offered again.
    if hasattr(state, "note_day_rejected") and state.note_day_rejected(text):
        logger.info("triage: caller ruled out a day; rebuilding the menu")

    # A named time books the visit. Bare consent deliberately does not: see
    # CallState.note_booking and run 262.
    if hasattr(state, "note_booking") and state.note_booking(text):
        logger.info(f"triage: appointment set for {state.appointment_iso}")

    # A booking is not the end of the conversation. Run 300's caller asked for
    # another day four times after his slot was confirmed and was read the same
    # closing sentence each time.
    if hasattr(state, "note_reschedule") and state.note_reschedule(text):
        logger.info(f"triage: appointment reopened/moved -> "
                    f"{state.appointment_iso or '(re-offering)'}")

    # He asked to be heard. Give him the turn back, and do not charge him for it.
    #
    # The refund is the half that fixes run 324. `location` was asked twice --
    # once normally, once while he was saying "listen to what I say" -- so the
    # two-ask budget was spent and the field dropped off the checklist for the
    # rest of the call. It was saved as null. An ask the caller was talked over
    # is not an ask he declined to answer, and the budget exists to stop
    # INTERROGATION (run 218), not to punish him for our own impatience.
    if result.wants_the_floor:
        state.wants_the_floor = True
        _refund_ask(state, "the caller asked to be heard")

    # "I have not told you that yet."
    #
    # The strongest signal there is, and it went unread until run 804. He said
    # it three times -- "ఏం బిల్ చెప్పలా", "నేను చెప్పలే కదా ఎందుకు ముందుకు
    # పోతున్నావ్", "బిల్లు తెలుసుకోలేదు కదా" -- while the field he was asking
    # for sat abandoned at its two-ask cap. Refunding puts it back in
    # `still_need`, which is the only thing that lets the agent ask it again.
    # Checked BEFORE the "you skipped me" branch: the two are one character
    # apart and the wrong reading costs the caller the same question again.
    elif CANNOT_ANSWER.search(text or ""):
        _abandon_ask(state, "the caller declined to answer it")

    elif NOT_YET_ANSWERED.search(text or ""):
        _refund_ask(state, "the caller says he has not answered it yet")

    # Same refund when we simply cut him off. If his final transcript stops on
    # a postposition or a topic marker, the sentence was still running when the
    # turn was ended -- so whatever we asked, he never got to finish answering.
    elif sounds_unfinished(text or ""):
        _refund_ask(state, "his sentence was still running")

    if result.next_step_agreed:
        state.next_step_agreed = True
    if result.buying_signal:
        state.buying_signal = True
    if result.no_more_questions:
        state.no_more_questions = True
    return result
