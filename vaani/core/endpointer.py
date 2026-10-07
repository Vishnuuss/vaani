"""Semantic endpointing -- deciding the caller has finished, without waiting.

The single biggest latency win available to us. A standard stack waits a fixed
600-800ms of silence before deciding the turn is over; Sarvam's own VAD defaults
to 500ms. That one number eats the whole budget before a vendor is called.

Humans do not wait for silence, they predict completion from grammar. So do we:

    "మా ఇల్లు..."      -> obviously unfinished -> wait 900ms
    "అవును సార్"        -> obviously finished   -> wait 120ms

This is the rules+lexicon baseline (plan §1.2 step 1). It ships first and gets
most of the win. The fine-tuned classifier replaces `classify()` later, trained
on real turn boundaries labelled from our own calls -- keep this interface
stable so that swap is a one-line change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Completion(Enum):
    COMPLETE = "complete"        # sentence looks finished
    CONTINUING = "continuing"    # trailing conjunction, clearly more coming
    MID_ENTITY = "mid_entity"    # in the middle of a number/name
    HESITATION = "hesitation"    # filler word, thinking
    EMPTY = "empty"


# Wait applied after the last audio frame before we commit the turn.
WAIT_MS = {
    Completion.COMPLETE: 120,
    Completion.CONTINUING: 700,
    Completion.MID_ENTITY: 900,
    Completion.HESITATION: 900,
    Completion.EMPTY: 1200,
}

# --- Telugu / Hindi lexicons -------------------------------------------------
# Trailing tokens that mean "I am not done": conjunctions, postpositions and
# subordinators that cannot legally end a spoken turn.
CONTINUING_SUFFIXES = (
    # Telugu
    "అయితే", "కానీ", "కాని", "మరియు", "అలాగే", "ఇంకా", "కాబట్టి", "ఎందుకంటే",
    "తర్వాత", "ముందు", "వరకు", "నుంచి", "నుండి", "గురించి", "కోసం", "వల్ల",
    # Hindi
    "लेकिन", "और", "क्योंकि", "इसलिए", "अगर", "तो", "फिर", "मगर",
)

# Hesitation / filler tokens -- the caller is thinking, not finished.
HESITATIONS = (
    "అంటే", "ఆ", "ఏమంటే", "అదే", "ఇంకా", "హ్మ్", "ఉమ్",
    "मतलब", "यानी", "हाँ वो", "उम", "अं",
    "um", "uh", "hmm", "aa",
)

# Tokens that commonly END a turn: agreement, refusal, short answers.
TERMINAL_TOKENS = (
    "అవును", "సరే", "లేదు", "ఒప్పుకున్నాను", "థాంక్స్", "ధన్యవాదాలు", "సార్", "అండి",
    "हाँ", "नहीं", "ठीक", "ठीक है", "धन्यवाद", "जी",
    "yes", "no", "ok", "okay", "sure", "thanks",
)

# Verb endings that finish a Telugu sentence (finite verb forms).
TELUGU_FINAL_VERB = re.compile(
    r"(ుంది|ాను|ాము|ారు|ావు|ింది|ాయి|తాను|తాము|తారు|దే|లేదు|ఉంది)$"
)

# A digit run in words -- callers reciting a phone number or an amount must not
# be cut off mid-sequence.
NUMBER_WORDS = (
    "ఒకటి", "రెండు", "మూడు", "నాలుగు", "ఐదు", "ఆరు", "ఏడు", "ఎనిమిది", "తొమ్మిది",
    "సున్నా", "పది", "వంద", "వేయి", "వేలు", "లక్ష", "కోటి",
    "एक", "दो", "तीन", "चार", "पांच", "छह", "सात", "आठ", "नौ", "शून्य",
    "सौ", "हज़ार", "लाख", "करोड़",
)


@dataclass
class Decision:
    completion: Completion
    wait_ms: int
    reason: str


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,;।!?\.]+", text.strip()) if t]


def classify(partial_text: str) -> Decision:
    """Judge whether a partial transcript looks like a finished turn.

    Pure function of the text -- no timers, no audio. The caller combines this
    with the actual silence clock.
    """
    text = (partial_text or "").strip()
    if not text:
        return Decision(Completion.EMPTY, WAIT_MS[Completion.EMPTY], "no speech yet")

    toks = _tokens(text)
    last = toks[-1] if toks else ""
    lowered = last.lower()

    # Order matters: the most specific "do not cut me off" signals win.
    if any(last.endswith(s) or lowered == s.lower() for s in CONTINUING_SUFFIXES):
        return Decision(Completion.CONTINUING, WAIT_MS[Completion.CONTINUING],
                        f"trailing conjunction {last!r}")

    if lowered in (h.lower() for h in HESITATIONS):
        return Decision(Completion.HESITATION, WAIT_MS[Completion.HESITATION],
                        f"hesitation {last!r}")

    # Mid-number: a digit word at the end usually means more digits follow.
    # "మూడు వేలు" (three thousand) is complete, so a magnitude word ends it.
    if last in NUMBER_WORDS and last not in ("వేలు", "వంద", "లక్ష", "కోటి",
                                             "सौ", "हज़ार", "लाख", "करोड़"):
        return Decision(Completion.MID_ENTITY, WAIT_MS[Completion.MID_ENTITY],
                        f"mid number run {last!r}")

    if lowered in (t.lower() for t in TERMINAL_TOKENS):
        return Decision(Completion.COMPLETE, WAIT_MS[Completion.COMPLETE],
                        f"terminal token {last!r}")

    if TELUGU_FINAL_VERB.search(last):
        return Decision(Completion.COMPLETE, WAIT_MS[Completion.COMPLETE],
                        f"finite verb ending {last!r}")

    # A single word that is none of the above is usually a fragment.
    if len(toks) < 2:
        return Decision(Completion.MID_ENTITY, WAIT_MS[Completion.MID_ENTITY],
                        "single-word fragment")

    # A short utterance with no terminal marker is almost always a fragment --
    # the caller is still going. Call 01fb4c93 committed on "మనం ఆ రక" and
    # "అవునా థ", both cut off mid-word, because the old default was 280ms.
    if len(text) < 12:
        return Decision(Completion.MID_ENTITY, WAIT_MS[Completion.MID_ENTITY],
                        "short fragment, no terminal marker")

    # Default: probably complete, but hedge. Being early is far worse than being
    # late -- talking over the caller destroys the call, a short pause does not.
    return Decision(Completion.COMPLETE, 600, "no continuation signal")


def wait_for(partial_text: str) -> int:
    """Convenience: milliseconds of silence to wait before committing."""
    return classify(partial_text).wait_ms
