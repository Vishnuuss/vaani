"""Understand what a Telugu caller just said about money.

The call this comes from
------------------------
Run 274, three turns, then the caller gave up:

    AGENT   సరే, మీ నెలవారీ బిల్లు ఎంత రూపాయలుగా ఉంటుంది?
    CALLER  వన్ లాక్ అండి
    AGENT   సరే, మీ నెలవారీ బిల్లు ఎంత రూపాయలుగా ఉంటుంది?
    CALLER  వన్ లాక్ అండి
    AGENT   సరే, మీ నెలవారీ బిల్లు ఎంత రూపాయలుగా ఉంటుంది?

Stored: `monthly_bill: null`. He answered the question twice, clearly, and the
agent could not hear an amount in it -- so it asked again, and again, which is
the single behaviour this project has spent days removing.

Why a parser and not a better prompt
------------------------------------
The amount was being read by the extraction LLM. That is the wrong tool for it,
for reasons that are not about model quality:

  - it is ASYNCHRONOUS, so its answer lands a turn late and the next question is
    chosen before it arrives
  - it is PROBABILISTIC, so the same sentence can parse today and not tomorrow
  - money is STRUCTURED, and structured extraction is the one thing a few
    hundred lines of deterministic code does better, faster and for free

So the amount is read here, synchronously, in microseconds, before the reply is
generated. The LLM keeps everything it is genuinely better at.

What these callers actually say
-------------------------------
Not "one hundred thousand rupees". Every real transcript on this project mixes
scripts and languages inside one phrase:

    వన్ లాక్ అండి              English numeral, English scale, Telugu particle
    ఒక లక్ష                    Telugu numeral, Telugu scale
    పది లక్షలు                  Telugu numeral, inflected scale
    టెన్ టు ట్వంటీ లాక్స్        an English RANGE in Telugu script
    ఫిఫ్టీ క్రోర్స్             English scale transliterated
    50,000 / ₹50,000           digits
    రెండు వేలు                  thousands
    10 to 15 లాక్స్             digits with a Telugu-script scale

A parser that only reads Telugu numerals, or only digits, misses most of them.
This reads numeral and scale independently and in either script, which is what
the transcripts require.

Ranges are answered honestly
-----------------------------
"టెన్ టు ట్వంటీ లాక్స్" is not 10 and it is not 20. It is a range, and run 218's
caller was asked three more times because something wanted a single figure. The
midpoint is returned along with the bounds, so the agent can accept the answer
and move on instead of interrogating a man who has already answered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Scale words, in both scripts and in the inflected forms Telugu actually uses.
SCALES: dict[str, int] = {
    "కోటి": 10_000_000, "కోట్లు": 10_000_000, "కోట్ల": 10_000_000,
    "క్రోర్": 10_000_000, "క్రోర్స్": 10_000_000, "crore": 10_000_000,
    "crores": 10_000_000, "cr": 10_000_000,
    "లక్ష": 100_000, "లక్షలు": 100_000, "లక్షల": 100_000, "లచ్చ": 100_000,
    "లాక్": 100_000, "లాక్స్": 100_000, "lakh": 100_000, "lakhs": 100_000,
    "lac": 100_000, "lacs": 100_000,
    "వెయ్యి": 1_000, "వేలు": 1_000, "వేల": 1_000, "వేయి": 1_000,
    "థౌజండ్": 1_000, "థౌసండ్": 1_000, "thousand": 1_000, "thousands": 1_000,
    "k": 1_000,
    "హండ్రెడ్": 100, "hundred": 100, "వంద": 100, "వందల": 100,
}

# Numerals. Telugu speakers on these calls use English for numbers more often
# than not, so both are first-class.
NUMERALS: dict[str, float] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "half": 0.5, "quarter": 0.25,
    "వన్": 1, "టు": 2, "త్రీ": 3, "ఫోర్": 4, "ఫైవ్": 5, "సిక్స్": 6,
    "సెవెన్": 7, "ఎయిట్": 8, "నైన్": 9, "టెన్": 10, "ట్వెల్వ్": 12,
    "ఫిఫ్టీన్": 15, "ట్వంటీ": 20, "థర్టీ": 30, "ఫోర్టీ": 40, "ఫిఫ్టీ": 50,
    "సిక్స్టీ": 60, "డెబ్బై": 70, "ఎనభై": 80, "తొంభై": 90,
    "ఒక": 1, "ఒకటి": 1, "రెండు": 2, "మూడు": 3, "నాలుగు": 4, "ఐదు": 5,
    "ఆరు": 6, "ఏడు": 7, "ఎనిమిది": 8, "తొమ్మిది": 9, "పది": 10,
    "పదకొండు": 11, "పన్నెండు": 12, "పదిహేను": 15, "ఇరవై": 20, "ముప్పై": 30,
    "నలభై": 40, "యాభై": 50, "అరవై": 60, "డెబ్భై": 70, "సగం": 0.5,
    # "వంద" is BOTH a numeral and a scale in Telugu -- "వంద కోట్లు" is a
    # hundred crores, while "ఐదు వందల" is five hundred. It appears in both
    # tables on purpose; the scale lookup runs first, so the multiplier reading
    # wins where one applies and this reading covers the rest.
    "వంద": 100, "హండ్రెడ్": 100, "hundred": 100,
}

# "టెన్ టు ట్వంటీ", "10 to 15", "పది నుంచి ఇరవై".
# A token SET, not a regex with . Word boundaries do not work here:
# "టు" ends in a combining vowel sign, which `\w` does not count as a word
# character, so `టు` never matches and every range silently collapsed to
# its upper figure. The same trap broke the tokeniser above.
RANGE_TOKENS = {"to", "టు", "నుంచి", "నుండి", "మధ్య", "-", "–"}

# A bill is monthly money. These say the caller is talking about something else.
NOT_MONEY = re.compile(
    r"(గంట|oclock|o'clock|బజే|కిలోవాట్|kilowatt|\bkw\b|యూనిట్|unit|"
    r"సంవత్సర|year|నెల\s*రోజు|శాతం|percent|%)", re.IGNORECASE)


# What a monthly electricity bill can credibly be.
#
# Run 286: the caller said "60 క్రోర్స్" and the agent answered "60 కోట్లు బిల్లు
# నిజంగా పెద్దది" -- gushing at six hundred million rupees a month. No such
# electricity bill exists anywhere; the largest industrial consumers in India
# are two orders below it. A salesperson who accepts that figure has stopped
# listening, and the caller knows it.
#
# The ceiling was 5 crore, and run 312 walked straight through it. The caller
# said "2,000 rupees"; Sarvam returned "రెండు కోట్లు" (2 crore); 20,000,000 sat
# inside the bound, so the `doubted` path never fired and the agent
# congratulated him on it -- "రెండు కోట్లు బిల్లు చాలా పెద్దది".
#
# The old comment justified the generous ceiling by saying that rejecting a real
# large bill is worse than accepting a silly one. That premise was simply wrong
# about what this gate does: it does not reject anything. An implausible figure
# costs ONE turn of "did you mean thousands or crores?" -- and a caller with a
# genuinely enormous bill is not offended by being asked to repeat it. Accepting
# a misheard one costs the integrity of the lead record silently.
#
# So the ceiling is now set from what rooftop solar actually addresses. At the
# ~Rs 8/unit industrial HT tariff, Rs 50 lakh a month is roughly 860 kW drawn
# continuously -- already at the top of what any rooftop array can serve. A bill
# above that is not this company's customer even when it is real, so confirming
# it costs nothing. The floor rules out a stray digit being read as a bill.
MAX_PLAUSIBLE = 5_000_000
MIN_PLAUSIBLE = 100

# The scale words Sarvam actually confuses with each other on phone audio.
# "వేలు" (thousands) heard as "కోట్లు" (crores) is a 10,000x error out of one
# syllable, and it is the single most damaging STT failure on this call type --
# so when a figure is implausible, the agent asks WHICH SCALE rather than asking
# vaguely to confirm. A generic "are you sure?" wastes the turn: the caller
# repeats the same word and Sarvam mishears it the same way again.
CONFUSABLE_SCALES = (10_000_000, 100_000, 1_000)


@dataclass(frozen=True)
class Amount:
    """A sum of money the caller named."""

    rupees: int
    low: int | None = None      # set when they gave a range
    high: int | None = None
    # The figure and the scale word it was attached to, kept separately so an
    # implausible reading can be re-offered at a smaller scale. "రెండు కోట్లు"
    # is figure=2, scale=10_000_000; the repair needs the 2, not the 20,000,000.
    figure: float | None = None
    scale: int | None = None

    @property
    def plausible(self) -> bool:
        """Could this really be somebody's monthly electricity bill?

        An implausible figure is not discarded -- the caller did say it, and it
        may be a mishearing worth checking. It is simply never recorded as fact
        and never reacted to as though it were.
        """
        return MIN_PLAUSIBLE <= self.rupees <= MAX_PLAUSIBLE

    @property
    def is_range(self) -> bool:
        return self.low is not None and self.high is not None

    def alternatives(self) -> list[str]:
        """The plausible readings of this figure at a smaller scale.

        Run 312's "రెండు కోట్లు" returns ["2 వేలు", "2 లక్షలు"] -- which is
        exactly the question worth asking, and the caller answers it in one
        word. Asking "are you sure?" instead gets the same misheard syllable
        back a second time.

        Empty when there is nothing to offer -- a bare figure with no scale
        word, or one where no smaller reading is plausible either. The caller is
        then asked to repeat the figure plainly.
        """
        if self.figure is None or self.scale is None or self.is_range:
            return []
        out = []
        for scale in CONFUSABLE_SCALES:
            if scale >= self.scale:
                continue
            value = int(round(self.figure * scale))
            if MIN_PLAUSIBLE <= value <= MAX_PLAUSIBLE:
                out.append(Amount(rupees=value).say())
        return out

    def say(self) -> str:
        """Read it back the way these callers say it, so it can be confirmed."""
        def part(v: int) -> str:
            if v >= 10_000_000:
                n = v / 10_000_000
                return f"{n:g} కోట్లు"
            if v >= 100_000:
                n = v / 100_000
                return f"{n:g} లక్షలు"
            if v >= 1_000:
                n = v / 1_000
                return f"{n:g} వేలు"
            return f"{v} రూపాయలు"
        if self.is_range:
            return f"{part(self.low)} నుంచి {part(self.high)}"
        return part(self.rupees)


def _numeral(tok: str) -> float | None:
    """One token as a number, in either script, or as digits."""
    if tok in NUMERALS:
        return NUMERALS[tok]
    m = re.fullmatch(r"[₹]?([\d,]+(?:\.\d+)?)", tok)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _numerals_before(tokens: list[str], i: int) -> float | None:
    """The number attached to the scale word at `tokens[i]`.

    Looks back at most two tokens, because "పది లక్షలు" and "టెన్ టు ట్వంటీ
    లాక్స్" both put the figure immediately before the scale, while anything
    further back belongs to a different clause.
    """
    # Compound numerals first: "ట్వంటీ ఫైవ్ థౌజండ్" is 25 thousand, not 5.
    # Reading only the nearest token turns 25,000 into 5,000 -- a twentyfold
    # error in a qualification figure, silently.
    if i >= 2:
        tens, unit = _numeral(tokens[i - 2]), _numeral(tokens[i - 1])
        if (tens is not None and unit is not None
                and tens >= 20 and tens % 10 == 0 and 1 <= unit <= 9):
            return tens + unit

    for j in (i - 1, i - 2):
        if j < 0:
            continue
        t = tokens[j]
        if t in NUMERALS:
            return NUMERALS[t]
        digits = re.fullmatch(r"[₹]?([\d,]+(?:\.\d+)?)", t)
        if digits:
            try:
                return float(digits.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def parse_amount(text: str) -> Amount | None:
    """The money in this sentence, or None if there is none.

    None is a real answer. Reading an amount out of "రేపు పది గంటలకు" would put
    a time in the bill field, which is the mirror of the bug that booked an
    appointment from "మూడు లక్షలు" (see booking.py).
    """
    t = (text or "").strip()
    if not t or NOT_MONEY.search(t):
        return None

    low = t.lower().replace("₹", " ₹")
    # Telugu is tokenised by its own Unicode block, not by `\w`. Vowel signs and
    # the virama are combining MARKS, which `\w` excludes, so a `\w+` tokeniser
    # splits "లక్షలు" into fragments and no scale word is ever matched -- which
    # is why run 274's "వన్ లాక్ అండి" produced nothing at all.
    tokens = re.findall(r"[₹]?[\d,]+(?:\.\d+)?|[ఀ-౿]+|[a-zA-Z]+", low)
    if not tokens:
        return None

    # Every (figure, scale) pair in the sentence, in order.
    found: list[int] = []
    pairs: list[tuple[float, int]] = []
    for i, tok in enumerate(tokens):
        scale = SCALES.get(tok)
        if scale is None:
            continue
        # "టెన్ టు ట్వంటీ లాక్స్" carries ONE scale word shared by both figures.
        # Read only the nearest numeral and the answer is 20 lakhs, not a range
        # of 10 to 20 -- which is how run 218's caller ended up being asked for
        # his bill three more times after he had already given it.
        if i >= 3 and tokens[i - 2] in RANGE_TOKENS:
            a, b = _numeral(tokens[i - 3]), _numeral(tokens[i - 1])
            if a is not None and b is not None:
                found.extend([int(round(a * scale)), int(round(b * scale))])
                continue
        n = _numerals_before(tokens, i)
        if n is None:
            continue
        found.append(int(round(n * scale)))
        pairs.append((n, scale))

    if not found:
        # A bare figure with no scale word: "50,000", "₹50000". Only accept it
        # when it is large enough to be a monthly bill -- a stray "2" in a
        # sentence is not an amount, and guessing costs the caller a re-ask.
        for tok in tokens:
            digits = re.fullmatch(r"[₹]?([\d,]+)", tok)
            if not digits:
                continue
            try:
                v = int(digits.group(1).replace(",", ""))
            except ValueError:
                continue
            if v >= 500:
                return Amount(rupees=v)
        return None

    if len(found) >= 2 and any(t in RANGE_TOKENS for t in tokens):
        lo, hi = min(found[0], found[1]), max(found[0], found[1])
        # The midpoint, so a caller who answers with a range is ACCEPTED rather
        # than asked again for a single figure -- which is what happened to run
        # 218's caller three times before he hung up.
        return Amount(rupees=(lo + hi) // 2, low=lo, high=hi)

    figure, scale = pairs[0] if pairs else (None, None)
    return Amount(rupees=found[0], figure=figure, scale=scale)
