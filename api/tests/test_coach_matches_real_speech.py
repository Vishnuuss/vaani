"""Coach rules must match what callers ACTUALLY say, not idiomatic Telugu.

Why this test exists
--------------------
Audited 7 Sep against 3,104 real caller utterances harvested from 2,097 runs:
**14 of the 34 CUES rules had never matched a single caller.** The coaching
layer that is supposed to steer the model at runtime was, in large part, inert.

The cause is one mistake repeated: each pattern was written the way a fluent
speaker would phrase the objection, and real callers are blunter than that while
Sarvam transliterates loanwords its own way.

    rule            written for              callers actually said
    rented          "రెంట్" (virama)          "రెంటెడ్"  (Sarvam's English)
    too_expensive   "రేటు ఎక్కువ"             "ముందు రేటు చెప్పండి"
    too_many_calls  "చాలా మంది కాల్"          "ఫోన్ చేయొద్దు అండి"
    is_it_free      "ఫ్రీనా"                  "free అని ఇస్తారా?"

The cost is concrete. Run 837's caller said he lives in a rented house twice, in
his first breath and again later. `రెంట్` cannot match `రెంటెడ్`, so the rented
cue never fired, he was never disqualified, and the agent asked his property
type, his bill, his area, whether he had a roof, invented a "shared roof access"
question, asked his name and offered him a free site survey. He hung up. The
prompt already says, in "When to stop": *if they do not own their roof, thank
them warmly and end the call.* The instruction was there. The signal that should
have triggered it was not.

This is the same failure as the turn detector's one-class training set: a
component validated against imagined data rather than real data, passing every
test it had, and wrong in production. The fix in both cases is to check against
the corpus.

The fixtures below are verbatim caller utterances from the harvest. Do not
"correct" their grammar or spelling -- being ungrammatical is the entire point.
"""

from __future__ import annotations

import re

import pytest

from api.services.vaani.coach import CUES


def _rule(name: str):
    for r in CUES:
        if getattr(r, "name", None) == name or getattr(r, "key", None) == name:
            return r
    raise AssertionError(f"no cue rule named {name!r}")


def _matches(rule, text: str) -> bool:
    for attr in ("pattern", "rx", "regex", "_rx"):
        v = getattr(rule, attr, None)
        if v is not None:
            rx = v if hasattr(v, "search") else re.compile(v)
            return bool(rx.search(text))
    raise AssertionError(f"cue {rule!r} exposes no pattern")


# Verbatim from .tmp/harvest — every one of these was said by a real caller.
REAL_SPEECH = [
    # run 837: said twice, matched neither, ended in a hangup after being
    # offered a site survey he had no roof for.
    ("rented", "మేము రెంటెడ్ హౌస్ లో ఉంటాం."),
    ("rented", "ఆహా లేదమ్మా మేము ఉండేది రెంటెడ్ హౌసు"),
    ("rented", "రెంట్ కే"),
    ("rented", "రెంట్, రెంట్."),
    ("rented", "we live in a rented house"),
    # Price demanded up front -- the same objection, arriving earlier.
    ("too_expensive", "ముందు రేటు చెప్పండి"),
    ("too_expensive", "వడ్డీ రేటు ఎంత ఉంటుంది చెప్పండి"),
    # Not an objection but an obligation.
    ("too_many_calls", "ఏం అవసరం లేదండి, మీరు మళ్ళీ మళ్ళీ కాల్ చేయొద్దండి."),
    ("too_many_calls", "ఫోన్ చేయొద్దు అండి"),
    ("too_many_calls", "నాకు లోన్ వద్దు, ఇక కాల్ చేయొద్దు"),
    # Sarvam leaves the English word in Latin script mid-sentence.
    ("is_it_free", "నాకు ఏం అవసరం లేదు వచ్చే free అని ఇస్తారా?"),
]


@pytest.mark.parametrize("name,text", REAL_SPEECH)
def test_cue_fires_on_real_speech(name, text):
    assert _matches(_rule(name), text), (
        f"cue {name!r} does not match a real caller utterance: {text!r}. "
        "A cue that cannot match real speech is inert, and its absence is "
        "silent -- the agent simply carries on qualifying."
    )


# Utterances that must NOT trigger, so the widened patterns cannot pass by
# matching everything.
MUST_NOT_MATCH = [
    # "రెండు" (two) is a near neighbour of "రెంట" and is a BILL answer. Confusing
    # them would disqualify every caller who says his bill is two thousand.
    ("rented", "రెండు వేలు."),
    ("rented", "సొంత ఇల్లే."),
    ("rented", "రెండు లక్షలు"),
    # A bare bill figure is not a price objection.
    ("too_expensive", "పదిహేడు వందలు"),
    ("too_expensive", "విజయవాడ."),
    # Agreeing to a call back is the opposite of asking not to be called.
    ("too_many_calls", "సరే, కాల్ చేయండి"),
    ("is_it_free", "నా పేరు నాగమణి."),
]


@pytest.mark.parametrize("name,text", MUST_NOT_MATCH)
def test_cue_does_not_over_match(name, text):
    assert not _matches(_rule(name), text), (
        f"cue {name!r} wrongly matched {text!r}. Widening a pattern until it "
        "fires is not a fix; it just moves the error."
    )


def test_the_rented_cue_distinguishes_rent_from_two():
    """The one confusion that would be actively harmful.

    "రెండు" (two) and "రెంట" (rent) differ by a single consonant, డ against ట,
    and "రెండు వేలు" (two thousand) is the single most common bill answer in the
    corpus. Matching the rent stem against it would disqualify the best-answering
    callers on the line.
    """
    rented = _rule("rented")
    assert _matches(rented, "రెంట్ కే")
    assert not _matches(rented, "రెండు వేలు.")
