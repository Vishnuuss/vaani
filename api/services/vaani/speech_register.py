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

# "ఐదు గంట" -> "ఐదు గంటలు". Telugu marks the plural on the noun, and a count
# above one takes it: ఒక గంట (one hour) is right, ఐదు గంట is not. Run 305 read
# out "ఉదయం తొమ్మిది గంట, మధ్యాహ్నం ఒక గంట, లేదా సాయంత్రం ఐదు గంట" -- the middle
# one correct by accident and the other two wrong.
#
# ఒక / one is excluded rather than special-cased away, because it is the one
# count that genuinely takes the singular.
_NUMERALS_PLURAL = (
    "రెండు|మూడు|నాలుగు|ఐదు|ఆరు|ఏడు|ఎనిమిది|తొమ్మిది|పది|పదకొండు|పన్నెండు|"
    "టు|త్రీ|ఫోర్|ఫైవ్|సిక్స్|సెవెన్|ఎయిట్|నైన్|టెన్|"
    r"two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+")
# A safety net for the same thing in text the MODEL wrote. It has seen
# "oclock" in the offer line all day and copies the pattern into its own
# sentences, where no amount of fixing booking.py reaches it.
_OCLOCK = re.compile(r"\s*o\s*['’]?\s*clock(?:\s*(?:కి|కు|కీ))?", re.IGNORECASE)

_HOURS = re.compile(rf"({_NUMERALS_PLURAL})(\s+)గంట(కు|కి)?(?![ల])")


def _hours(text: str) -> str:
    def fix(m):
        case = m.group(3)
        noun = "గంటలకు" if case else "గంటలు"
        return f"{m.group(1)}{m.group(2)}{noun}"
    return _HOURS.sub(fix, text)


# "విష్ణు అండి" -> "విష్ణు గారు".
#
# అండి is a sentence-final politeness particle -- it goes after a VERB, closing
# what you just said ("చెప్పండి", "ఉందండి"). The particle that goes after a
# NAME is గారు. "విష్ణు అండి" is not merely informal, it is wrong, and a Telugu
# speaker hears it immediately: run 305's agent said it four times.
#
# Detected structurally rather than from a list of names, which there could
# never be: a token that is followed by అండి and is NOT itself a Telugu word
# ending in a verb suffix is being addressed, not spoken to. The safer and
# narrower test used here is the one the caller supplies -- `spoken()` takes the
# names in play and rewrites only those, so an ordinary "చెప్పండి" is untouched
# because it is one token, not two.
_ANDI = "అండి"

# The name-based rewrite above is necessary and NOT sufficient, and run 320
# shows exactly why:
#
#     USER : మా పేరు విష్ణు అండి
#     BOT  : మంచిది విష్ణు అండి, ఉచిత సైట్ అసెస్‌మెంట్ ...
#
# The name reaches `spoken()` from `state.known["customer_name"]`, which the
# ASYNCHRONOUS extractor fills one turn later. So on the single turn where the
# agent first uses the name -- the turn that matters -- there is no name to
# match against, and `_names` has nothing to do. `_caller_names` even documents
# the empty tuple as "correct rather than a gap". It is a gap.
#
# So this catches the construction instead of the word. An agent addressing
# somebody opens with an acknowledgement, then the name, then the particle.
# Nothing else in this agent's speech has that shape.
#
# The middle token is excluded when it ends in -ండి, because that is the Telugu
# imperative ending: "సరే చెప్పండి అండి" is a verb being politely closed, and
# rewriting it to "చెప్పండి గారు" would be a new bug of the same kind.
_ACK_BEFORE_NAME = "|".join(
    ["మంచిది", "సరే", "థాంక్యూ", "అర్థమైంది", "అవును", "ఓకే", "సరేనండి"])
# Words that are already a form of address. గారు does not stack on top of them.
_ALREADY_HONORIFIC = {"సార్", "మేడమ్", "అయ్యా", "అమ్మ", "అమ్మా", "గారు", "బాబు"}

_VOCATIVE_ANDI = re.compile(
    rf"(?:{_ACK_BEFORE_NAME})\s+([ఀ-౿]+)\s*{_ANDI}(?![ఀ-౿])"
)


def _vocative(text: str) -> str:
    """"మంచిది విష్ణు అండి" -> "మంచిది విష్ణు గారు", with no list of names."""
    def swap(m: "re.Match[str]") -> str:
        word = m.group(1)
        if word.endswith("ండి"):        # an imperative verb, not a name
            return m.group(0)
        if word in _ALREADY_HONORIFIC:
            # "సార్ గారు" stacks two honorifics, which is its own kind of wrong
            # -- and worse than the అండి it would be replacing, because it reads
            # as servile rather than merely ungrammatical.
            return m.group(0)
        return m.group(0).replace(f"{word} {_ANDI}", f"{word} గారు").replace(
            f"{word}{_ANDI}", f"{word} గారు")
    return _VOCATIVE_ANDI.sub(swap, text)


def _names(text: str, names: tuple[str, ...]) -> str:
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        text = text.replace(f"{name} {_ANDI}", f"{name} గారు")
        text = text.replace(f"{name}{_ANDI}", f"{name} గారు")
        text = text.replace(f"{name} గారు అండి", f"{name} గారు")
    return text


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


def spoken(text: str, names: tuple[str, ...] = ()) -> str:
    """Rewrite one piece of generated speech into the register people use.

    `names` are the people in the conversation -- the caller, usually -- so
    "విష్ణు అండి" can be corrected to "విష్ణు గారు" without a list of every
    Telugu name in existence.
    """
    if not text:
        return text
    out = _hours(_ranges(_OCLOCK.sub(" గంటలకు", text)))
    out = _vocative(out)
    if names:
        out = _names(out, names)
    for literary, said in SPOKEN:
        if literary in out:
            out = out.replace(literary, said)
    return out
