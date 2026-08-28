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


def check(reply: str, *, allow_price: bool = False,
          closing: bool = False, caller_said: str = "") -> GuardrailReport:
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
})


def blocking(report: "GuardrailReport") -> list["Violation"]:
    """The violations that justify replacing the reply rather than logging it."""
    return [v for v in report.violations if v.rule in BLOCKING_RULES]


# What the reference agent says instead of asking the identical question again:
# "క్షమించండి సరిగ్గా వినిపించలేదు, మళ్ళీ చెప్తారా". Measured on
# Downloads/AISORIGIN_VIDEO/apgovt.mpeg -- it never repeats a sentence, and when
# it mishears it says so. Run 96 asked the same question four times word for
# word and the caller answered "you told me nothing".
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
