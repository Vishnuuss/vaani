"""Just-in-time sales coaching: deep training at zero steady-state prompt cost.

The problem this solves
-----------------------
Training Layers 1 and 2 from 26,216 to 112,526 characters made the agent better
and made it unusable. Run 336 measured the LLM at 1.884s against 0.337s, and a
bench with real history reproduced it: 1.070s -> 2.099s, hidden reasoning 91 ->
160 characters. A reasoning model reads every instruction before it answers, so
instruction volume IS latency. Compressing the layers back under their original
size recovered the speed and cost some of the depth.

Both are wanted. The way to have both is to stop paying for every rule on every
turn.

Almost all of a sales playbook is conditional. The line about what to do when
someone says the price is too high is worth a lot on the one turn they say it,
and is dead weight on the other fifteen. So the catalogue lives HERE, out of the
system prompt, and one or two rows are selected by pattern on the caller's own
words and appended to the state block.

Why the state block and not the system prompt
----------------------------------------------
`compiler.py` puts Layers 1+2 at the FRONT precisely so they land in Groq's
cached prefix -- byte-identical across every turn and every call, billed at half
price and, more importantly, not re-read. Anything varying per turn must NOT go
there or the cache misses on every turn. `brain_processor._refresh` appends the
state block as a trailing system message, which is both uncached already and the
most authoritative position in the context. That is where this goes.

The cost
--------
`MAX_LINES` rows at `MAX_CHARS` characters. About 220 characters on the turns
that match, nothing on the turns that do not -- against 86,310 characters had
this been written into the layers. The catalogue below can grow to any size
without that number moving.

Precision matters more than recall, for the same reason it does in `triage`:
a wrong row is worse than no row, because the model obeys the trailing block
over the prose above it. When nothing matches, the METHOD in Layer 2 already
covers it -- it is written to generalise to objections not in any list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Two is the ceiling on purpose. Three tactical instructions in a block that
# also carries STILL_NEED and the next question is how a two-sentence reply
# becomes a paragraph.
MAX_LINES = 2
MAX_CHARS = 260


@dataclass(frozen=True)
class Cue:
    """One trigger and the coaching it earns.

    `weight` orders the selection when a turn matches several. Emotion outranks
    objection: how they said it changes the shape of the reply, and the content
    of the reply is useless in the wrong shape.
    """

    name: str
    pattern: re.Pattern
    say: str
    weight: int = 50


def _c(name: str, source: str, say: str, weight: int = 50) -> Cue:
    return Cue(name, re.compile(source, re.IGNORECASE), say, weight)


# ---------------------------------------------------------------------------
# TONE -- how they said it. Weighted above every objection row.
#
# "it should catch the tones and general real human like, see if cost high it
# should react" -- the client, 3 Sep. A reaction that does not contain the fact
# is not a reaction; Layer 1 carries that rule, and these carry the trigger.
# ---------------------------------------------------------------------------

# A number the caller themselves called large. Never fires on a number the
# agent produced -- this only ever reads the caller's utterance.
_BIG_MONEY = (
    r"(లక్ష|లక్షల|వేల|వేలు|thousand|lakh)"
    r"[^.!?]{0,25}"
    r"(దాటు|పైన|ఎక్కువ|భారీ|చాలా|కష్ట|మోయ|ఎగిరి|పెరిగ|over|above|huge)"
    r"|(దాటు|పైన|ఎక్కువ|భారీ|చాలా|పెరిగ)[^.!?]{0,25}(లక్ష|వేల|thousand|lakh)"
)

TONE: tuple[Cue, ...] = (
    _c("bill_shock", _BIG_MONEY,
       "REACT FIRST: name their number back in two or three words "
       "(అది పెద్ద amount సార్ / నెలకి అంత అంటే ఎక్కువే), THEN your question. "
       "Skipping the reaction is what makes you sound like a form.", 95),
    _c("angry",
       r"(కోపం|చిరాకు|విసుగు|ఏం\s*పీక|నోరు\s*ముయ్|దండగ|బూతు|చెత్త|వేస్ట్"
       r"|irritat|annoy|nonsense|shut\s*up|bloody|waste\s+of\s+(my\s+)?time)",
       "He is angry. Say NOTHING about the product this turn. Let him finish, "
       "apologise once, offer to stop calling. Never defend, never explain.", 99),
    _c("hurry",
       r"(తొందర|బిజీ|టైమ్\s*లేదు|మీటింగ్|డ్రైవింగ్|తర్వాత\s*మాట్లాడ|అర్జెంట్"
       r"|busy|driving|in\s+a\s+meeting|no\s+time|make\s+it\s+quick)",
       "He is in a hurry. ONE short sentence, then one closed question with "
       "two times in it. No pitch, no explanation, no second question.", 92),
    _c("suspicious",
       r"(నిజంగా|నిజమేనా|అనుమానం|ఎలా\s*నమ్మ|నమ్మకం\s*లేదు|నమ్మలేను"
       r"|గ్యారెంటీ\s*ఏంటి|catch|ట్రిక్"
       r"|really\s*\?|how\s+do\s+i\s+(trust|know)|prove)",
       "He does not trust you yet. Be concrete: company name, where the number "
       "came from, and offer removal before he asks. No claim you cannot back.", 90),
    _c("confused",
       r"(అర్థం\s*కాలే|అర్థం\s*కావట|ఏంటి\s*అది|ఏం\s*చెప్పాలి|తెలీదు|తెలియదు"
       r"|అంటే\s*ఏంటి|ఏం\s*అంటున్నారు"
       r"|didn.?t\s+(get|understand)|what\s+do\s+you\s+mean|no\s+idea)",
       "He did not understand the QUESTION, not the offer. Ask it again a "
       "shorter way WITH the options named. Do not explain the product.", 94),
    _c("sad",
       r"(చనిపోయ|ఆసుపత్రి|హాస్పిటల్|జబ్బు|అనారోగ్య|కష్టం\s*గా\s*ఉంది|నష్టం"
       r"|passed\s+away|hospital|not\s+well|ill\b)",
       "Bad news. One short warm sentence, then STOP -- do not ask anything "
       "this turn. Selling into this loses the person, not just the call.", 98),
    _c("joking",
       r"(హహ|హాహా|నవ్వ|జోక్|సరదా|haha|hehe|lol|just\s+kidding)",
       "He made a joke. A short warm one back, then continue. Ignoring a joke "
       "is the single most robotic thing you can do.", 70),
)

# ---------------------------------------------------------------------------
# OBJECTIONS -- what they said. The METHOD lives in Layer 2 and always applies;
# these carry the part of the answer that is specific to this objection.
# ---------------------------------------------------------------------------

OBJECTIONS: tuple[Cue, ...] = (
    _c("too_expensive",
       r"(ఖరీదు|కాస్ట్లీ|రేటు\s*ఎక్కువ|ధర\s*ఎక్కువ|డబ్బు\s*ఎక్కువ|భారం"
       r"|too\s+(expensive|costly)|price\s+is\s+high)",
       "Do not argue it is cheap and do not discount unasked. Set it against a "
       "cost he ALREADY pays every month, using his own figure. One rebuttal.", 80),
    _c("no_money",
       r"(డబ్బు\s*లేదు|స్తోమత|తాహతు|ఆర్థికంగా|అంత\s*లేదు|పైసలు\s*లేవు"
       r"|can.?t\s+afford|no\s+money)",
       "Accept it at once and warmly -- his dignity is on the line. No payment "
       "plan unless he asks, no second attempt. Close kindly.", 96),
    _c("not_interested",
       r"(ఆసక్తి\s*లేదు|ఇంట్రెస్ట్\s*లేదు|అవసరం\s*లేదు|వద్దు\s*అండి|వద్దండి"
       r"|not\s+interested|don.?t\s+need)",
       "Reflex, said before he knows what it is. ONE gentle probe naming the "
       "one concrete thing he gains. If he refuses again in any form, END.", 85),
    _c("think_about_it",
       r"(ఆలోచించి|ఆలోచిస్తా|చూద్దాం|చెప్తాను\s*తర్వాత|later\s+i.?ll"
       r"|think\s+about\s+it|let\s+me\s+see)",
       "This is a no for today. Thank him by name and end warmly. There is no "
       "version of asking again that converts this -- asking loses the referral.", 88),
    _c("call_later",
       r"(తర్వాత\s*కాల్|మళ్ళీ\s*కాల్\s*చేయండి|ఇప్పుడు\s*కుదరదు|రేపు\s*చేయండి"
       r"|call\s+(me\s+)?(back|later))",
       "Take the exit, but make it concrete: offer TWO specific times, never "
       "an open question. If he names neither, thank him and end.", 84),
    _c("whatsapp",
       r"(వాట్సాప్|వాట్సప్|మెసేజ్\s*పంపండి|డీటెయిల్స్\s*పంపండి"
       r"|whats\s*app|send\s+(me\s+)?(details|message))",
       "Agree immediately -- never refuse. Then attach ONE question so the "
       "message you send is actually about his situation.", 82),
    _c("ask_family",
       r"(భార్య|ఆయన|ఆవిడ|కుటుంబం|అబ్బాయి|అమ్మాయి|ఇంట్లో\s*అడగాలి|నాన్నగారు"
       r"|wife|husband|family|ask\s+(my\s+)?(son|daughter))",
       "Real and legitimate. Support it out loud, never treat them as an "
       "obstacle, and offer a time when BOTH are there.", 86),
    _c("already_have",
       r"((ఇప్పటికే|అల్రెడీ|already)[^.!?]{0,25}(ఉంది|ఉన్నాయి|పెట్టు|వేయించు)"
       r"|already\s+(have|got|installed))",
       "Ask ONE question about what he already has. Never attack it -- the man "
       "chose it. If it genuinely covers him, say so and close warmly.", 87),
    _c("competitor",
       r"(వేరే\s*కంపెనీ|ఇంకో\s*కంపెనీ|another\s+company|someone\s+else\s+"
       r"(called|quoted))",
       "Never criticise them by name. Ask what he was quoted or promised, and "
       "answer only the gap. Running them down makes you the smaller company.", 83),
    _c("who_are_you",
       r"(మీరు\s*ఎవరు|ఎక్కడి\s*నుంచి|ఏ\s*కంపెనీ|నంబర్\s*ఎక్కడ|ఎలా\s*వచ్చింది"
       r"|who\s+(are|is)\s+(you|this)|which\s+company|where\s+did\s+you\s+get)",
       "Company name, the REAL source of his number from your business facts, "
       "and offer removal before he asks. Never invent a source.", 91),
    _c("too_many_calls",
       r"(చాలా\s*మంది\s*కాల్|రోజూ\s*కాల్|ప్రతిరోజూ\s*ఫోన్"
       r"|too\s+many\s+calls|everyone\s+keeps\s+calling)",
       "That is a complaint, not an objection. Apologise once, offer removal, "
       "and do not sell into it.", 93),
    _c("is_it_free",
       r"(ఫ్రీనా|ఉచితమా|డబ్బు\s*కట్టాలా|ఛార్జ్\s*ఉందా|ఎంత\s*కట్టాలి"
       r"|is\s+it\s+free|any\s+charge)",
       "Answer plainly and immediately -- yes or no and why, in one sentence. "
       "Any vagueness here confirms he was right to suspect you.", 89),
    _c("rented",
       r"(అద్దె|రెంట్|సొంతం\s*కాదు|అద్దెకు\s*ఉంటు"
       r"|rented|not\s+my\s+(house|place)|on\s+rent)",
       "Do not push past this. Ask whether the owner would consider it, and if "
       "not, thank him and close -- he is not the decision-maker.", 88),
    _c("guarantee",
       r"(గ్యారెంటీ|వారంటీ|ఎన్నేళ్ళు|ఎంతకాలం|రిపేర్|మెయింటెనెన్స్|సర్వీస్"
       r"|guarantee|warranty|how\s+many\s+years|maintenance)",
       "This is a BUYING signal, not an objection. Give only what your business "
       "facts state, never a number you invented, then move to the visit.", 81),
)

# ---------------------------------------------------------------------------
# BUYING -- outranks the checklist. Layer 2 says a missed buying signal costs
# the customer; this is the trigger that catches it in the caller's own words.
# ---------------------------------------------------------------------------

BUYING: tuple[Cue, ...] = (
    _c("come_and_see",
       r"(వచ్చి\s*చూడండి|ఒకసారి\s*రండి|వస్తారా|చూసి\s*చెప్పండి"
       r"|come\s+(and\s+)?(see|have\s+a\s+look)|visit\s+us)",
       "He just asked for the sale. STOP qualifying. Offer two times, get one "
       "chosen, confirm it in his words, and end the call.", 97),
    _c("price_question",
       r"(ఎంత\s*(అవుతుంది|అవుద్ది|అవ్తుంది|ఖర్చు|పడుతుంది|ఉంటుంది)"
       r"|ధర\s*ఎంత|రేటు\s*ఎంత|కాస్ట్\s*ఎంత"
       r"|how\s+much|what.?s\s+the\s+(price|cost))",
       "A price question is a BUYING signal, not an objection. Never defer it. "
       "Say what it DEPENDS on -- that is a real answer -- and hand the question "
       "straight back in the same breath.", 78),
    _c("how_does_it_work",
       r"(ఎలా\s*పని\s*చేస్తుంది|ఎలా\s*ఉంటుంది|ప్రాసెస్\s*ఏంటి|ఎంత\s*టైమ్\s*పడుతుంది"
       r"|how\s+does\s+it\s+work|what.?s\s+the\s+process|how\s+long)",
       "A process question is a buying signal. Two plain sentences answering "
       "it, then your one question. Never answer it with a question.", 79),
)

# ---------------------------------------------------------------------------
# SEGMENT -- who the caller is, 4 Sep.
#
# "anything i ask from website it should answer perfectly ... it should explain
# in simple way" -- the client.
#
# The first attempt at this wrote the whole of a client's service catalogue
# into the node prompt: 9,811 -> 14,904 characters, and the eval's
# verbatim-repetition failures went from 1 to 8. Instruction volume is not
# free, and a drowning model repeats itself. So it sits here instead, one row
# chosen by the caller's own words, costing nothing on the turns it misses.
#
# These rows are the KIND OF PLACE the caller is calling about, which is the
# same set in every industry -- a society, a flat, a shed, a hospital, a
# factory, an office. `test_the_catalogue_carries_no_industry_vocabulary`
# holds this file to the same rule as Layers 1, 2 and 4: no trade words. What
# each segment actually BUYS is Layer 3's job, per client. What this supplies
# is the thing that makes a caller feel heard -- that you know what their kind
# of place spends money on -- and the instruction to say it in one line and
# hand the turn straight back.
#
# Weighted below TONE deliberately: a fact delivered in the wrong shape to an
# angry or hurried caller is worse than no fact.
# ---------------------------------------------------------------------------

SEGMENT: tuple[Cue, ...] = (
    _c("segment_society",
       r"(సొసైటీ|సొసైటి|అసోసియేషన్|కాలనీ|లిఫ్ట్|కారిడార్|కామన్\s*ఏరియా"
       r"|society|residents?\s+association|\bRWA\b|\bGHS\b|common\s+area|lift)",
       "Their spend is the SHARED bill -- lifts, water pumps, corridor lights, "
       "security -- not any one home. Name that in one line, it shows you "
       "understood, then ask your question.", 76),
    _c("segment_apartment",
       r"(అపార్ట్|ఫ్లాట్|టాప్\s*ఫ్లోర్|apartment|\bflat\b|top\s+floor)",
       "A shared building needs a specialist, not a general contractor, and "
       "that is exactly what matching them is for. One line, then your "
       "question.", 76),
    _c("segment_warehouse",
       r"(వేర్\s*హౌస్|గోడౌన్|గిడ్డంగి|warehouse|godown|logistics|cold\s*stor)",
       "A large open shed is the easy case and they should hear that, and that "
       "the structure is not put at risk. One line, then your question.", 75),
    _c("segment_institution",
       r"(స్కూల్|కాలేజ్|కాలేజీ|హాస్పిటల్|ఆసుపత్రి|కళాశాల|school|college"
       r"|hospital|clinic|institut)",
       "A hospital cannot lose power for a second, so it needs backup. A "
       "campus is a daytime load, which is the easy case. Use the one they "
       "said, one line, then your question.", 75),
    _c("segment_industry",
       r"(ఫ్యాక్టరీ|పరిశ్రమ|ఇండస్ట్రీ|మిల్లు|ప్లాంట్|factory|industr|\bmill\b"
       r"|manufacturing\s+unit)",
       "For them the electricity bill is a LARGE share of running cost, which "
       "is why it is worth their time. Say that, quote no percentage, then ask "
       "your question.", 75),
    _c("segment_office",
       r"(ఆఫీస్|ఆఫీసు|ఆఫిస్|office|showroom|shop\b)",
       "Air conditioning and computers are the load, and the real worry is "
       "disruption -- say the work happens without stopping their day. One "
       "line, then your question.", 74),
    _c("segment_land",
       r"(ఖాళీ\s*(స్థలం|ల్యాండ్|భూమి)|ల్యాండ్|భూమి|స్థలం|పొలం"
       r"|open\s+land|vacant\s+land|\bacres?\b)",
       "They have opened a bigger option than the one you asked about. Ask "
       "roughly how much land, and carry on -- do not price it and do not "
       "promise what can be built.", 74),
    _c("careers",
       r"(ఉద్యోగ|జాబ్|కొలువు|ఇంటర్న్|రిక్రూట్|అప్లై"
       r"|job|jobs|hiring|vacancy|vacancies|career|internship|resume|\bCV\b)",
       "This is a job call, not a sale. STOP qualifying immediately. Take name "
       "and number, say the team will call back, end warmly. Never discuss "
       "salary or promise an interview -- you do not know.", 88),
    _c("vendor_check",
       r"(ఎలా\s*(verify|వెరిఫై|చెక్|నమ్మ)|నమ్మకమైన|వెండర్స్\s*ఎలా"
       r"|how.{0,20}(verified|checked)|genuine|verified\s*ఎలా|trustworthy)",
       "They are checked and approved before they are listed -- credentials "
       "and real reviews -- and the caller sees past work before choosing. One "
       "line, then your question.", 78),
    _c("response_time",
       r"(ఎప్పుడు\s*(కాల్|call|ఫోన్|phone|కాంటాక్ట్|contact|వస్తారు|చేస్తారు)"
       r"|ఎంత\s*టైమ్\s*లో|ఎప్పటిలోగా"
       r"|when\s+will\s+(they|someone|i|we)|how\s+soon|how\s+long\s+will\s+it\s+take)",
       "Give a real window, not 'soon' -- usually a few hours, most within a "
       "day. Then confirm the number you are going to pass on.", 77),
)


CUES: tuple[Cue, ...] = TONE + OBJECTIONS + BUYING + SEGMENT


def cues_for(text: str) -> list[Cue]:
    """Every cue whose trigger appears in this utterance, strongest first."""
    if not (text or "").strip():
        return []
    return sorted((c for c in CUES if c.pattern.search(text)),
                  key=lambda c: -c.weight)


def coach(text: str, exclude: object = ()) -> list[str]:
    """The lines to append to the state block for this utterance.

    `exclude` is the set of cue names already coached in this call. A tactic
    repeated on a caller who did not take it the first time is the "one
    rebuttal, never two" rule being broken by the prompt itself.
    """
    seen = set(exclude or ())
    out: list[str] = []
    for cue in cues_for(text):
        if cue.name in seen:
            continue
        line = f"COACH ({cue.name}): {cue.say}"
        if len(line) > MAX_CHARS:
            continue
        out.append(line)
        seen.add(cue.name)
        if len(out) >= MAX_LINES:
            break
    return out
