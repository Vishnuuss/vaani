"""Does what the caller just said sound like a whole thought?

Why the audio model is not enough
---------------------------------
`TeluguTurnAnalyzer` reads the tail of the waveform: energy falling away, pitch
dropping, the acoustic shape of someone finishing. That signal is real and it
carries most of the decision. It is also blind to one whole class of
unfinished utterance, and it is the class these callers produce constantly.

    CALLER   "అరవై"                     sixty
    (0.3 s while he decides)
    CALLER   "డెబ్భై అనుకుంటా"           ...seventy, I think

"అరవై" is one falling word. Acoustically it is indistinguishable from "సరే"
(fine) -- same length, same trailing energy, same pitch contour. Prosody cannot
separate them because the difference is not in the sound. It is that a bare
number is a quantity the speaker has not finished assembling, and "సరే" is an
answer.

So the text is read too, and the two are combined: either one saying "wait"
extends the turn.

What this deliberately is NOT
------------------------------
Not a list of the phrases one caller happened to say. Every rule here is a
grammatical CLASS, so it generalises to callers who have never rung:

    a dangling quantity      "60", "అరవై", "పది"        -- a unit is coming
    an open range            "10 to", "పది నుంచి"        -- the top is coming
    a connective             "కానీ", "ఎందుకంటే", "and"   -- a clause is coming
    a hesitation             "ఆ", "uhh", "మ్మ్"          -- a word is coming

The cost of being wrong is bounded and small
--------------------------------------------
A True here does not hold the turn open indefinitely. It raises the floor to
`fragment_floor_secs` (0.45 s) -- and any real speech in that window resets the
silence timer, which is the entire mechanism. A false positive costs the caller
under half a second; a false negative costs him the sentence he was saying. The
asymmetry is why the rules lean towards waiting.
"""

from __future__ import annotations

import re

from api.services.vaani.amounts import NUMERALS, RANGE_TOKENS, SCALES

# A sound, not a word. These are what a speaker emits while the next word is
# still arriving -- bare vowels and nasals, in either script. ElevenLabs test
# their endpointer by injecting exactly these; run 295's "ఆ మరి ఆ." is one, and
# the agent answered it as though it were the monthly bill.
#
# "ఆ" and "ఏ" are also real Telugu words (that / which). Treating them as
# hesitation when they stand alone at the END of an utterance is the right
# reading anyway: a demonstrative with nothing after it is not a finished
# sentence either.
HESITATIONS = {
    "ఆ", "అ", "ఏ", "ఓ", "ఊ", "ఈ", "ఉ", "ఇ", "ఎ", "ఒ",
    "హా", "హం", "అం", "ఆం", "మ్మ్", "హ్మ్", "మ్", "అబ్బ",
    "uh", "uhh", "um", "umm", "uhm", "ah", "aa", "aaa", "er", "err",
    "hmm", "hm", "hmmm", "mm", "mmm", "eh", "oh",
    # Sarvam writes English fillers in Telugu script, and until run 314 none of
    # these were here. The caller said "um... Bhaskar"; the transcript read
    # "ఉమ్ భాస్కర్"; the name was stored as "ఉమ్ భాస్కర్" and the agent spent the
    # rest of the call addressing him as **"ఉమ్ భాస్కర్ గారు"** -- "Mr. Um
    # Bhaskar".
    #
    # "ఉమ్" carries a virama, so it ends in a bare consonant and is not a Telugu
    # word. The NAME Uma is "ఉమ"/"ఉమా" and is deliberately NOT in this set.
    "ఉమ్", "అమ్", "హ్మ్మ్", "ఎర్", "ఆహ్", "ఊమ్", "ఏమ్",
}

# Words whose grammar promises another clause. A sentence cannot stop on one.
CONNECTIVES = {
    "కానీ", "కాని", "ఎందుకంటే", "అయితే", "ఇంకా", "ఇంక", "తర్వాత", "తరువాత",
    "మరియు", "లేదా", "మరి", "అప్పుడు", "అలాగే", "ఇంకో", "ఇంకొక", "ఒకవేళ",
    "and", "but", "or", "so", "because", "if", "then", "means", "like",
    "actually", "basically", "means", "అండ్", "బట్", "సో",
}

# Trailing punctuation only. Deliberately NOT stripping politeness particles:
# "60 అండి" ("it's 60") IS a finished answer and must not be flagged, and the
# particle is exactly what tells us so.
_PUNCT = "?.!,;:। \t\n\"'"

_DIGITS = re.compile(r"^[₹]?[\d,]+(?:\.\d+)?$")


def _tokens(text: str) -> list[str]:
    r"""Split on whitespace, then strip punctuation from each side.

    A regex tokeniser is not used here on purpose. `\w` excludes Telugu
    combining vowel signs (Unicode category Mn), so `` lands in the middle of
    a syllable and words ending in a vowel sign -- which is most of them --
    never match. That trap silently broke every range in `amounts.py` once
    already; whitespace has no such opinion about Telugu.
    """
    return [t for t in (w.strip(_PUNCT) for w in (text or "").split()) if t]


def sounds_unfinished(text: str) -> bool:
    """True when the caller is audibly still assembling the sentence."""
    tokens = _tokens(text)
    if not tokens:
        # Silence with no words is the analyzer's problem, not this one.
        return False

    low = [t.lower() for t in tokens]

    # Nothing but hesitation: "ఆ మరి ఆ", "umm... uh". There is no answer here
    # yet, however confident the waveform sounds.
    if all(t in HESITATIONS or t in CONNECTIVES for t in low):
        return True

    last = low[-1]

    if last in HESITATIONS:
        return True

    # A connective is a promise of another clause.
    if last in CONNECTIVES:
        return True

    # "పది నుంచి" / "10 to" -- the top of the range has not been said.
    if last in RANGE_TOKENS:
        return True

    # A dangling quantity: a number with no unit, scale or particle after it.
    # This is the "60 ... aaa ... 70" case, and the reason run 295 stored a
    # monthly bill of 62.
    if (_DIGITS.match(last) or last in NUMERALS) and last not in SCALES:
        return True

    return False


def strip_fillers(text: str) -> str:
    """Remove hesitation noise from the EDGES of a value about to be stored.

    Run 314 is why this exists. `customer_name` was stored as "ఉమ్ భాస్కర్" and
    the agent addressed a real customer as "Mr. Um Bhaskar" for the rest of the
    call. The hesitation vocabulary above already knew what a filler was -- it
    had simply never been consulted anywhere except the turn-taking decision.

    Edges only, never the middle. A filler between two words was said inside a
    phrase the caller meant, and cutting there would splice two halves of a
    sentence into something he never said. At the edges it is always noise.

    Returns the original text if stripping would leave nothing -- a bare "ఉమ్"
    is a real answer to "is anyone there?", and an empty string stored as a name
    is worse than a wrong one because nothing downstream can see it happened.
    """
    if not text or not text.strip():
        return text
    toks = text.split()
    lo, hi = 0, len(toks)
    while lo < hi and toks[lo].strip(_PUNCT).lower() in HESITATIONS:
        lo += 1
    while hi > lo and toks[hi - 1].strip(_PUNCT).lower() in HESITATIONS:
        hi -= 1
    if lo >= hi:
        return text
    return " ".join(toks[lo:hi]).strip(_PUNCT)
