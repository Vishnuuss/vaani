"""Deterministic compliance checks on a drafted reply.

Hard rules do not belong in prose. The 30-persona run on 2026-08-25 proved it:
the prompt says "never promise a guaranteed return" in bold, and the agent still
promised one to a caller who pushed four times. Prose degrades under pressure;
a regex does not.

`ReplyFilter._gate` runs this on each chunk against everything spoken so far,
BEFORE that chunk reaches TTS, so a violation is caught as it completes rather
than after the caller has heard the reply. Cheap (microseconds), so it costs
nothing on the critical path -- unlike a second model call, which is why the
escalation path is deliberately narrow.

Only `BLOCKING_RULES` replace a reply. The rest are recorded at the end of the
response and left in place: cutting a caller off mid-sentence for a stray
asterisk would be worse than the asterisk.

This claimed to run before TTS long before it did. Until 2026-08-27 the only
caller ran it on `LLMFullResponseEndFrame`, after the audio had streamed, and
merely logged -- which is why SAFE_FALLBACK, SAFE_CLOSE and correction_note had
no callers at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Violation:
    rule: str
    evidence: str
    correction: str          # appended to the retry prompt


# --- guarantee / promised return -------------------------------------------
# Telugu and English forms of "I guarantee", "definitely will save", "sure to".
_GUARANTEE = re.compile(
    r"(గ్యారెంటీ|గారంటీ|హామీ|కచ్చితంగా\s+(ఆదా|తగ్గు|వస్తుంది|సేవ్)"
    r"|ఖచ్చితంగా\s+(ఆదా|తగ్గు)"
    r"|guarantee|guaranteed|assured\s+return|definitely\s+save)",
    re.IGNORECASE)

# A guarantee DENIAL is fine and must not be flagged: "నేను గ్యారెంటీ ఇవ్వలేను".
_GUARANTEE_DENIAL = re.compile(
    r"(గ్యారెంటీ|గారంటీ|హామీ|guarantee)\s*\S{0,12}\s*"
    r"(ఇవ్వలేను|ఇవ్వను|ఇవ్వడం\s+కుదరదు|లేదు|cannot|can't|won't|do not|don't)",
    re.IGNORECASE)

# --- specific price quote ---------------------------------------------------
# Rupee amounts of 4+ digits, or Telugu number-words for large sums attached to
# a cost word. Bills the CUSTOMER stated are fine; quoting OUR price is not.
_PRICE = re.compile(
    r"(₹\s*\d{4,}|rs\.?\s*\d{4,}|\d{4,}\s*(రూపాయలు|rupees)"
    r"|(ఖర్చు|కాస్ట్|ధర|price|cost)\D{0,20}(\d{4,}|లక్ష|లక్షలు))",
    re.IGNORECASE)


# --- interrogation detector --------------------------------------------------
# Regex over the CALLER's words cannot generalise across industries -- what a
# jewellery customer says to book is nothing like what a solar customer says.
# The agent's own draft can, because "did I just ask a question" is the same
# test in every industry and every language we support.
_QUESTION = re.compile(
    r"[?？]"
    r"|(ఎంత|ఎప్పుడు|ఎక్కడ|ఎవరు|ఎలా|ఏమి|ఏం|ఎందుకు|ఏదైనా"
    r"|ఉందా|ఉన్నారా|చేస్తారా|చెప్తారా|వస్తారా|కుదురుతుందా|తీసుకుంటారా|అవునా|సరేనా)"
    r"|(क्या|कब|कितना|कैसे|कहाँ|कौन)"
    r"|(what|when|where|which|who|why|how much|how many"
    r"|do you|are you|can you|could you|would you|shall we)",
    re.IGNORECASE)


# --- markdown leaking into speech -------------------------------------------
# Layer 1 bans markdown because the text goes straight to a speech engine, which
# reads "**" aloud. The jewellery run produced a bulleted, bold-formatted list
# mid-call. Prose did not stop it; this does.
_MARKDOWN = re.compile(r"(\*\*|__|^\s*[-*+]\s+|^#{1,6}\s+|`|^\s*\d+\.\s+)",
                       re.MULTILINE)

# --- amounts written as English words ---------------------------------------
# Numbers are now spoken in English words on purpose (Layer 1), which quietly
# blinded the digit-based price check above -- "five thousand rupees" matched
# nothing. This closes that hole: a magnitude word next to money or a quantity.
_NUM_WORD = (r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
             r"twelve|fifteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|"
             r"ninety)")
B = "(?<![A-Za-z])"
E = "(?![A-Za-z])"
_PRICE_WORDS = re.compile(
    B + _NUM_WORD + r"[ ]+(?:hundred|thousand|lakh|crore)s?" + E
    + "|" + B + r"(?:hundred|thousand|lakh|crore)s?[ ]+(?:rupees|rs|రూపాయలు)"
    + "|" + B + _NUM_WORD + r"[ ]+(?:hundred[ ]+|thousand[ ]+)?(?:grams?|గ్రాములు|tolas?)" + E,
    re.IGNORECASE)


@dataclass
class GuardrailReport:
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def correction_note(self) -> str:
        if self.ok:
            return ""
        return ("Your previous reply broke a hard rule. Rewrite it in one or two "
                "sentences, fixing ONLY this: "
                + " ".join(v.correction for v in self.violations))


_NUMBER_WORD = re.compile(
    r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand|lakh|"
    r"ఒక|రెండు|మూడు|నాలుగు|ఐదు|వంద|వేలు|వెయ్యి|లక్ష)", re.IGNORECASE)


def _echoes_a_number(reply: str, caller_said: str) -> bool:
    """Does the reply only contain numbers the CALLER already said?"""
    mine = {m.group(0).lower() for m in _NUMBER_WORD.finditer(reply)}
    theirs = {m.group(0).lower() for m in _NUMBER_WORD.finditer(caller_said)}
    return bool(mine) and mine <= theirs


# --- invented quantities -----------------------------------------------------
# Run 318, verbatim:
#
#   USER : మా ఫ్యాక్టరీ వచ్చేసి 300 స్క్వేర్ మీటర్స్ ... ఎన్ని సోలార్ ప్యానెల్స్ కావాలి?
#   BOT  : 300 square meters రూఫ్ మీద సుమారు 30 kW సిస్టమ్ పెట్టవచ్చు,
#          అంటే 80-100 panels అవసరం అవుతాయి.
#
# Nobody told it 30 kW or 80-100 panels. It did the arithmetic itself, on a call,
# for a factory owner who will repeat the figure to a vendor. Layer 2 has said
# "Never manufacture a specific" since the beginning and the model ignored it,
# which is this project's most repeated lesson: prose loses to a gate.
#
# A BLACKLIST cannot work here. Banning "kW" would break the best answer the
# agent has -- "మొదటి 2 kW కి 30,000 rupees per kW ... 78,000 వరకు" is the PM
# Surya Ghar subsidy, it is correct, and it came from the knowledge base.
#
# So: a WHITELIST. A number the agent says must appear either in the knowledge
# base or in what the caller just said. Anything else it made up. The whitelist
# is derived from the client's own compiled prompt, so a new client's numbers
# are allowed automatically with no code change.
_QUANTITY_UNIT = re.compile(
    r"(?:kw|kilowatt|kwp|mw|panels?|ప్యానెల్స్|ప్యానెళ్ళు|units?|యూనిట్లు|"
    r"sq\.?\s*(?:ft|m)|square\s+(?:feet|foot|meters?|metres?)|"
    r"స్క్వేర్\s*(?:ఫీట్|మీటర్)|చదరపు|percent|%|శాతం|years?|సంవత్సరాల?|ఏళ్ళు)",
    re.IGNORECASE)

# Digits, or English/Telugu number words. Ranges ("80-100") split into both ends
# on purpose: an invented range is two invented numbers, and quoting only its
# top would slip through a check that looked at the string as a whole.
_ANY_NUMBER = re.compile(
    r"\d[\d,]*(?:\.\d+)?"
    r"|(?<![A-Za-z])(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|"
    r"lakh|lakhs|crore|crores)(?![A-Za-z])"
    r"|ఒక|ఒకటి|రెండు|మూడు|నాలుగు|ఐదు|ఆరు|ఏడు|ఎనిమిది|తొమ్మిది|పది|ఇరవై|ముప్పై"
    r"|నలభై|యాభై|అరవై|వంద|వందల|వెయ్యి|వేలు|వేల|లక్ష|లక్షలు|కోటి|కోట్లు",
    re.IGNORECASE)


def numbers_in(text: str) -> set[str]:
    """Every number token in `text`, normalised for comparison."""
    out = set()
    for m in _ANY_NUMBER.finditer(text or ""):
        token = m.group(0).lower().replace(",", "").rstrip(".")
        if token.isdigit():
            # "30,000" and "30000" are the same claim, and "078" is 78. Strip
            # leading zeros but never to nothing -- an earlier version turned
            # the "000" of a comma-split "30,000" into "0" and then reported it
            # as an invented quantity in the subsidy answer.
            token = token.lstrip("0") or "0"
        out.add(token)
    return out


def invented_quantities(reply: str, allowed: set[str]) -> list[str]:
    """Numbers the reply states next to a unit that nobody supplied.

    Only sentences carrying a UNIT are examined. A bare number in ordinary
    speech -- "ఒక నిమిషం", "రెండు ఆప్షన్స్" -- is not a technical claim, and
    flagging those would fire on almost every turn.
    """
    out = []
    for sentence in re.split(r"[.!?।\n]+", reply or ""):
        if not _QUANTITY_UNIT.search(sentence):
            continue
        for token in numbers_in(sentence):
            if token not in allowed:
                out.append(token)
    return out


def check(reply: str, *, allow_price: bool = False,
          closing: bool = False, caller_said: str = "",
          known_numbers: set[str] | None = None) -> GuardrailReport:
    """Inspect a drafted reply. Fast, deterministic, no model call.

    `closing` comes from `must_close(state)`. When it is set, the call is over
    and any further question is a failure -- this is the single most common one
    in testing: the agent hears "I already have it" or "book me in", agrees
    warmly, and then asks its next checklist question anyway.
    """
    report = GuardrailReport()
    text = reply or ""

    if closing and _QUESTION.search(text):
        report.violations.append(Violation(
            rule="no_questions_when_closing",
            evidence=_QUESTION.search(text).group(0),
            correction=("The call is over. Do not ask anything at all. Say one "
                        "short, warm closing sentence and stop."),
        ))

    if _GUARANTEE.search(text) and not _GUARANTEE_DENIAL.search(text):
        report.violations.append(Violation(
            rule="no_guarantee",
            evidence=_GUARANTEE.search(text).group(0),
            correction=("Do not promise or imply any guaranteed saving, return or "
                        "outcome. Say that it depends on their own situation "
                        "and that the next step is what settles it."),
        ))

    if _MARKDOWN.search(text):
        report.violations.append(Violation(
            rule="no_markdown",
            evidence=_MARKDOWN.search(text).group(0).strip() or "list formatting",
            correction=("You are speaking on a phone, not writing a document. "
                        "No asterisks, no bullet points, no headings, no "
                        "numbered lists. Say it in one or two plain sentences."),
        ))

    # Sentences, not paragraphs. Layer 1 caps a turn at two.
    sentences = [x for x in re.split(r"[.!?।\n]+", text) if len(x.strip()) > 12]
    if len(sentences) > 3:
        report.violations.append(Violation(
            rule="too_long",
            evidence=f"{len(sentences)} sentences",
            correction=("Far too long for a phone call. Rewrite as at most two "
                        "short sentences, then stop."),
        ))

    # Repeating the caller's OWN figure back is acknowledgement, not a quote.
    # A live reply was cut in half for saying "రెండు thousand rupees" -- the
    # bill the caller had just stated. The rule exists to stop the agent
    # INVENTING a price, and echoing their number invents nothing.
    echoing_them = bool(caller_said) and _echoes_a_number(text, caller_said)
    if not allow_price and not echoing_them and (
            _PRICE.search(text) or _PRICE_WORDS.search(text)):
        report.violations.append(Violation(
            rule="no_price_quote",
            evidence=(_PRICE.search(text) or _PRICE_WORDS.search(text)).group(0),
            correction=("Do not quote a price or total cost on the call. Say the "
                        "next step is what settles the exact figure."),
        ))

    # An EMPTY whitelist means no knowledge base was compiled, not that every
    # number is forbidden. Enforcing against nothing would gag the agent on its
    # first sentence, and a client with no facts yet is the one case where the
    # agent has least to lose by staying quiet about numbers on its own.
    if known_numbers:
        allowed = known_numbers | numbers_in(caller_said)
        made_up = invented_quantities(text, allowed)
        if made_up:
            report.violations.append(Violation(
                rule="no_invented_quantity",
                evidence=", ".join(sorted(set(made_up))),
                correction=(
                    "You stated a number nobody gave you. Do not size a system, "
                    "count panels, or estimate any quantity yourself. Say it "
                    "depends on the site assessment and that the vendor gives "
                    "the exact figure."),
            ))

    return report


# Rules worth cutting a reply off mid-sentence for. A caller hearing a clause
# stop short is strange; a caller hearing an invented price, a promise we cannot
# keep, or another question right after we agreed to leave them alone is a
# compliance problem. The rest (markdown, length) are cosmetic and the sanitizer
# already bounds length, so they stay advisory.
BLOCKING_RULES = frozenset({
    "no_questions_when_closing",
    "no_price_quote",
    "no_guarantee",
    # Run 318 told a factory owner his 300 sq m roof takes "30 kW ... 80-100
    # panels". He will repeat that to a vendor, and it came from nowhere. That
    # is a compliance failure of the same kind as an invented price, so it gets
    # the same treatment: the reply is replaced, not logged.
    "no_invented_quantity",
})


def blocking(report: "GuardrailReport") -> list["Violation"]:
    """The violations that justify replacing the reply rather than logging it."""
    return [v for v in report.violations if v.rule in BLOCKING_RULES]


# What the reference agent says instead of asking the identical question again:
# "క్షమించండి సరిగ్గా వినిపించలేదు, మళ్ళీ చెప్తారా". Measured on
# Downloads/AISORIGIN_VIDEO/apgovt.mpeg -- it never repeats a sentence, and when
# it mishears it says so. Run 96 asked the same question four times word for
# word and the caller answered "you told me nothing".
# The model writes its own version of the repair line, and when it does it
# treats the apology as permission to move on. Run 314:
#
#   BOT : సారీ, మీరు ఏ ఏరియా లేదా సిటీలో ఉన్నారో వినిపించలేదు.
#         మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?
#
# It apologised for not hearing the CITY and then asked about the PROPERTY. The
# caller never got to answer the city, and "సంతై" -- a fragment of a later
# sentence -- was stored as his location. He had said "మంచిర్యాల్లో"
# (Mancherial) twice by then.
#
# Matching the model's own phrasing, not just REPAIR_LINE, because REPAIR_LINE
# is only reached when the repetition guard fires.
SAID_NOT_HEARD = re.compile(
    r"(వినిపించలేదు|వినపడలేదు|అర్థం\s*కాలేదు|సరిగ్గా\s*విన"
    r"|couldn'?t\s+(hear|catch)|did\s*n[o']?t\s+(hear|catch))",
    re.IGNORECASE)

REPAIR_LINE = (
    "క్షమించండి, సరిగ్గా వినిపించలేదు. "
    "కొంచెం నెమ్మదిగా మళ్ళీ చెప్తారా అండి?"
)


SAFE_FALLBACK = (
    "సార్, కరెక్ట్ ఫిగర్ ఇప్పుడే చెప్పలేను. "
    "మా టీమ్ నుంచి కచ్చితమైన డీటెయిల్ చెప్పిస్తాను, సరేనా?"
)

# Used when the call must close and the draft kept interrogating.
SAFE_CLOSE = "సరే సార్, మీ టైమ్ ఇచ్చినందుకు థాంక్యూ. మంచి రోజు సార్."


def must_close(state) -> bool:
    """Is the agent forbidden from asking anything further this turn?

    Deliberately NOT true for a bare buying signal -- there the agent still has
    to ask which time suits, and that question is the close.
    """
    return bool(getattr(state, "must_end", False)
                or getattr(state, "disqualified", False)
                or getattr(state, "next_step_agreed", False)
                or getattr(state, "no_more_questions", False))
