"""Say it the way a Telugu speaker says it, not the way it is written.

The call this comes from
------------------------
Run 300. The agent was asked how many hours solar panels work and answered:

    సౌర ప్యానెల్స్ రోజుకు సుమారు eight నుండి ten గంటల వరకు శక్తి ఉత్పత్తి
    చేస్తాయి, మేఘావృత రోజుల్లో కూడా కొంచెం తక్కువగా పనిచేస్తాయి.

Every content word in that sentence is Sanskrit-derived literary Telugu.
Nobody selling anything down a phone line says సౌర, or ఉత్పత్తి, or మేఘావృత.
They say సోలార్, జనరేట్, మబ్బులు. The sentence is grammatically perfect and
socially wrong, which is worse than a small mistake -- it is the register of a
government notice, and it tells the customer immediately that they are not
talking to a salesperson.

Why this is code and not another prompt line
---------------------------------------------
Layer 1 has carried a "bookish Telugu is the main way this sounds like a
machine" section, with a worked table of wrong/right pairs, since the persona
was written. The model follows it for verbs and generic phrasing and then
reaches for Sanskrit the moment a technical noun is needed -- because that is
what Telugu TEXT contains. The written corpus is news, literature and
officialese; spoken Telugu is heavily code-mixed with English and is barely
written down at all. No amount of instruction outweighs the training
distribution on the one word the model needs right now.

It also said ధన్యవాదాలు, which is in the table by name as a thing not to say.

So the register is enforced after generation rather than requested before it.
This costs nothing: it is a dictionary lookup on a string that is already in
hand, it adds no tokens to the prompt (the state block is the uncached tail and
every character there is re-billed on every turn), and it cannot be ignored.

The rules this follows
-----------------------
Substitutions are noun-for-noun and phrase-for-phrase only. Verb morphology is
left alone -- Telugu agglutinates, and a verb swapped in isolation produces a
sentence that is wrong in a new and less predictable way than the one it fixed.
Everything here is general spoken Telugu, not solar vocabulary: విద్యుత్ is the
literary word for electricity in any industry, and the man paying the bill calls
it కరెంట్.
"""

from __future__ import annotations

import re

# Ordered: longer phrases first, so "సౌర ప్యానెల్స్" is matched before "సౌర"
# and the result is not "సోలార్ ప్యానెల్స్" arrived at by two separate passes.
SPOKEN: tuple[tuple[str, str], ...] = (
    # Solar and electricity. Sanskrit in the corpus, English on the phone.
    ("సౌర ఫలకాలు", "సోలార్ ప్యానెల్స్"),
    ("సౌర ఫలకాల", "సోలార్ ప్యానెల్స్"),
    ("సౌర ప్యానెల్స్", "సోలార్ ప్యానెల్స్"),
    ("సౌర విద్యుత్", "సోలార్"),
    ("సౌర శక్తి", "సోలార్"),
    ("సౌర", "సోలార్"),
    ("విద్యుత్ బిల్లు", "కరెంట్ బిల్లు"),
    ("విద్యుత్", "కరెంట్"),
    # Weather and generation.
    ("మేఘావృత రోజుల్లో", "మబ్బులు ఉన్న రోజుల్లో"),
    ("మేఘావృత", "మబ్బులు ఉన్న"),
    # Before the bare ఉత్పత్తి rules, or this leaves "శక్తి జనరేట్ చేస్తాయి" --
    # half-fixed, which reads worse than either register on its own.
    ("శక్తి ఉత్పత్తి", "పవర్ జనరేట్"),
    ("ఉత్పత్తి చేస్తాయి", "జనరేట్ చేస్తాయి"),
    ("ఉత్పత్తి చేస్తుంది", "జనరేట్ చేస్తుంది"),
    ("ఉత్పత్తి", "జనరేషన్"),
    # Register slips Layer 1 already names and the model produces anyway.
    ("ధన్యవాదాలు", "థాంక్యూ"),
    ("అందుబాటులో ఉంటారా", "కుదురుతుందా"),
    ("ఇష్టపడుతున్నారు", "కావాలి"),
    ("తెలియజేస్తాను", "చెప్తాను"),
    ("ప్రారంభించ", "స్టార్ట్ చేయ"),
)

# "eight నుండి ten గంటల వరకు" -> "eight to ten గంటలు".
#
# నుండి ("from") is a perfectly ordinary word -- "Hyderabad నుండి" is right --
# so it is not replaced on sight. Only the range frame is, and it is anchored on
# a number either side so an ordinary "from" can never match it. The trailing
# వరకు ("up to") goes with it: saying both halves of a written range aloud is
# what makes the sentence sound read rather than spoken.
_RANGE = re.compile(
    r"([\wఀ-౿]+)\s+నుండి\s+([\wఀ-౿]+)\s+"
    r"([ఀ-౿]+?)\s*ల?\s+వరకు")


def _ranges(text: str) -> str:
    return _RANGE.sub(lambda m: f"{m.group(1)} to {m.group(2)} {m.group(3)}లు", text)


def spoken(text: str) -> str:
    """Rewrite one piece of generated speech into the register people use."""
    if not text:
        return text
    out = _ranges(text)
    for literary, said in SPOKEN:
        if literary in out:
            out = out.replace(literary, said)
    return out
