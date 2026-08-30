"""Did the caller actually say no?

Run 312 is the whole reason this exists. The agent asked
"మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?" -- do you have your own roof or terrace.
The caller said "ఫ్యాక్టర్ పైన" (on the factory), which Sarvam then garbled
further to "ఎక్కడండి ట్రాక్టర్ పైన మా ట్రాక్టర్ పైన". Nothing in either
utterance is a "no". `మా X పైన` -- "on our X" -- is an AFFIRMATIVE: he is
telling you WHERE the roof is, and "మా" (our) is a possessive.

The extractor returned `roof_available: false` anyway. It had reasoned, quite
sensibly for a language model, that a tractor is not a roof and therefore the
answer must be no. The agent then said "మీకు రూఫ్ లేకపోవడం వల్ల సోలార్ సాధ్యం
కాదు" -- solar is not possible for you -- and hung up on a factory owner.

That is the worst outcome this system can produce, and it happened on the turn
immediately after the agent said out loud "మీరు చెప్పినది బాగా వినిపించలేదు" --
I could not hear you. It did not hear the answer, said so, and disqualified him
on it anyway.

The rule this module enforces
------------------------------
A NEGATIVE fact is only ever recorded from an explicit negation. The absence of
a yes is not a no. A garbled utterance is not a no. An answer to a different
question is not a no.

This is deliberately deterministic rather than another line in the extractor
prompt. The prompt already says "Never infer or guess" and the model guessed
anyway -- a boolean field is an invitation to guess, because two of the three
honest answers (yes / no / did-not-say) look alike to a model that wants to be
helpful. Prose cannot fix that; a gate can.

Asymmetric on purpose
----------------------
Only the FALSE direction is gated. A wrongly-recorded "yes" costs a site visit
that finds no roof. A wrongly-recorded "no" ends the call and the lead is gone
with no way to notice. The costs are not remotely symmetric, so the checks
are not either.
"""

from __future__ import annotations

import re

# Explicit negations, in the forms these callers actually use. Telugu marks
# negation with a verb, not a particle, so these are verb forms rather than a
# single word: లేదు (is not / does not exist), కాదు (is not so), వద్దు (do not
# want), లేను/లేము/లేవు (person-inflected forms of లేదు).
# Telugu marks negation with a verb, not a particle, and those verbs
# AGGLUTINATE: "రూఫ్ లేదండీ" and "లేదుసార్" are each a single token, so these
# have to match as substrings. That is safe for this set -- they are multi-
# syllable verb stems that do not occur inside unrelated words.
NEGATIONS_WITHIN = (
    "లేదు", "లేవు", "లేను", "లేము", "లేద",          # is not / does not exist
    "కాదు", "కాద",                                   # is not so
    "వద్దు", "అక్కర్లే",                              # do not want / not needed
    "లేకపో", "లేకుండా",                              # without / failing to
    "ఉండదు", "రాదు", "కాలేదు",                       # will not be / does not come
)

# Whole-token matches only. These are short and WOULD hit inside unrelated
# words as substrings -- "నో" sits inside "నోట్" (note) and "మనోహర్", and "not"
# sits inside "another" and "note". Sarvam writes English negatives in Telugu
# script constantly ("నో నో నో ఐ డోంట్ హావ్" is a real transcript from this
# project), so the Telugu-script spellings are first-class here, not an
# afterthought.
NEGATIONS_TOKEN = (
    "no", "nope", "not", "dont", "doesnt", "nothing", "never", "negative",
    "nahi", "nahin", "ledu", "kaadu", "kadu", "nope",
    "నో", "నాట్", "డోంట్", "నెవర్",
    "అద్దె",                                          # rented -- a real "not mine"
)

# Phrases, matched against the whitespace-joined token stream.
NEGATIONS_PHRASE = (
    "అవసరం లేదు", "ఏమీ లేదు", "అస్సలు లేదు", "సొంతం కాదు", "ఇష్టం లేదు",
    "don't", "doesn't",
)

# Words that make a "no" mean something other than an answer to the question --
# "no problem", "no doubt". Rare, but they read as negations to a substring
# match and they are not.
_NOT_A_NO = re.compile(
    r"\bno\s+(problem|doubt|issue|worries)\b|"
    r"ఇబ్బంది లేదు|సమస్య లేదు|అభ్యంతరం లేదు",
    re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    r"""Telugu by its own Unicode block, not by `\w`.

    Vowel signs and the virama are combining MARKS, which `\w` excludes, so a
    `\w+` tokeniser shreds "లేదు" into pieces and no negation ever matches.
    The same trap has now been hit in amounts.py, booking.py and
    completeness.py; it is written down in each of them for a reason.
    """
    return re.findall(r"[ఀ-౿]+|[a-zA-Z']+", (text or "").lower())


def is_bare_denial(text: str) -> bool:
    """A "no" with nothing in it -- no subject, no object, no fact.

    "కాదు." "లేదు." "no." One or two tokens, every one of them a negation.

    Run 324 is why this exists. The agent asked a THREE-WAY question -- own
    house, apartment or commercial -- and the caller answered "కాదు". The
    extractor read that as "no roof", disqualified him and ended the call at
    64 seconds, with `roof_available` null and the roof question never asked.

    A bare denial is an answer to whatever was last asked and it carries no
    statement of its own. It cannot be the evidence for a decision that ends
    the call, because it does not say what is being denied. "మాకు ఇప్పటికే
    సోలార్ ఉంది" -- we already have solar -- does say, and is unaffected.
    """
    toks = _tokens(text)
    if not toks or len(toks) > 2:
        return False
    if _NOT_A_NO.search(text or ""):
        return False
    return all(
        tok in NEGATIONS_TOKEN or any(neg in tok for neg in NEGATIONS_WITHIN)
        for tok in toks)


def is_negative(text: str) -> bool:
    """Does this utterance contain an actual refusal or denial?"""
    if not text or not text.strip():
        return False
    if _NOT_A_NO.search(text):
        return False
    toks = _tokens(text)
    if not toks:
        return False
    joined = " ".join(toks)

    if any(phrase in joined for phrase in NEGATIONS_PHRASE):
        return True
    if any(tok in NEGATIONS_TOKEN for tok in toks):
        return True
    return any(neg in tok for tok in toks for neg in NEGATIONS_WITHIN)
