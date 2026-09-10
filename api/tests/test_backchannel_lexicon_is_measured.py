"""The lexicon is now mined from real calls, so lock what the mining decided.

`tools/mine_backchannels.py` read all 2,097 run logs and counted every short
caller utterance together with how often it directly followed a bot QUESTION.
That second number is the discriminator, and it produced two conclusions that
are easy to undo by accident:

1. The frequent short Telugu utterances that are NOT already backchannels are
   all CONTENT -- "సొంతమే" (it's owned, 79), "చెప్పండి" (tell me, 102), "లేదు"
   (no, 17) -- each said in answer to a question 85-100% of the time. Putting
   any of them in BACKCHANNELS makes the agent deaf to an answer. The next
   person asked to "add more backchannel words" will reach for exactly these.

2. A whole class was missing from BOTH lexicons: the caller signalling the
   CHANNEL has failed. "హలో" is the single most common thing anyone says to
   this agent -- 381 times, three times the next -- and a caller saying
   "hello?" while the bot talks cannot hear it. Talking on is the worst
   available response.

These tests are the counts turned into assertions.
"""

from api.services.vaani.barge_in import (
    BACKCHANNELS,
    BargeInGate,
    BargeInParams,
    has_interrupt_word,
    is_backchannel,
)

# --- 1. acknowledgements, which must NOT stop the bot ---------------------

ACKNOWLEDGEMENTS = [
    "ఆ", "ఆఁ", "హా", "అవును", "సరే", "ఓకే", "ఊఁ", "అలాగే", "ఆహా", "ఉమ్",
    "మ్మ్", "ఆం", "ఊం", "హూ", "సరేలే", "ఓకె",
    "aa", "haa", "hmm", "mhm", "acha", "achha", "sari", "aan", "hoon",
    "umm", "yup", "sure", "correct", "fine", "okay", "right",
]


def test_every_acknowledgement_is_a_backchannel():
    """The words the client named -- "haa", "avunu", "sare" -- and their kin."""
    missed = [w for w in ACKNOWLEDGEMENTS if not is_backchannel(w)]
    assert not missed, f"these should not stop the bot: {missed}"


def test_the_lexicon_did_not_shrink():
    """A floor, not an exact count, so adding a spelling stays cheap."""
    assert len(BACKCHANNELS) >= 60


# --- 2. content answers, which must NEVER be treated as noise -------------
#
# Each of these was said 9-102 times in the run logs, in answer to a question
# 85-100% of the time. Mistaking one for a backchannel loses a real answer.

CONTENT_ANSWERS = [
    "సొంతమే",        # 79  -- "it's owned"
    "చెప్పండి",       # 102 -- "go ahead / tell me"
    "అవునండి",       # 49  -- "yes" WITH the polite particle: a real answer
    "లేదండి",        # 19  -- "no"
    "ఉంది",          # 9   -- "I have"
    "రెండు",         # 2   -- "two"
    "సొంతం",         # 2
    "home loan",
    "personal loan",
    "3bhk",
]


def test_a_content_answer_is_never_a_backchannel():
    wrong = [w for w in CONTENT_ANSWERS if is_backchannel(w)]
    assert not wrong, (
        f"these are ANSWERS, not noise; gating them loses the reply: {wrong}")


# --- 3. the class the hand-written list missed ----------------------------

MUST_INTERRUPT = [
    "హలో",                       # 381 -- he cannot hear us
    "hello",
    "ఏమన్నారు",                  # 23  -- "what did you say"
    "ఏంది",                      # 16
    "ఏంటమ్మా",                   # 18
    "ఎవరు",                      # 19  -- "who is this"
    "అర్థం కాలేదు",              # 9   -- "I didn't understand"
    "నాకు ఇంట్రెస్ట్ లేదు",       # 16  -- not interested
    "ఇంట్రెస్ట్ లేదు",            # 24
    "నాకు call చేయొద్దు",         # 26  -- do NOT call: a compliance event
    "ఆగండి మాట్లాడనివ్వట్లేదు",   # 25  -- "you aren't letting me speak"
    "వద్దు అన్నాను కదా",          # 10  -- "I said no, didn't I"
    "నాకు మనిషితో మాట్లాడాలి",    # 13  -- "I want a human"
    "ఆగండి", "ఆపండి", "వద్దు", "ఒక్క నిమిషం",
]


def test_every_stop_signal_lands():
    missed = [w for w in MUST_INTERRUPT if not has_interrupt_word(w)]
    assert not missed, f"these MUST stop the bot instantly: {missed}"


def test_a_stop_signal_is_never_gated_as_a_backchannel():
    wrong = [w for w in MUST_INTERRUPT if is_backchannel(w)]
    assert not wrong, f"gated a stop signal: {wrong}"


# --- 4. the ordering that makes the above true ---------------------------


def _gate(text: str, secs: float = 0.05) -> str:
    """A gate with the bot speaking, a hostile duration floor, and `text`."""
    g = BargeInGate(BargeInParams(enabled=True, min_speech_secs=10.0))
    g.note_bot_speaking(True)
    g.note_speech_started(started_at=0.0)
    g.note_text(text)
    return g.should_interrupt(now=secs).reason


def test_a_stop_word_beats_the_duration_floor():
    """"ఆగండి" is three syllables and lands under any floor worth having.

    The lexical check runs FIRST for exactly this reason. If it ever moves
    below the duration check, a caller saying "don't call me" waits for the bot
    to finish its paragraph.
    """
    assert _gate("నాకు call చేయొద్దు") == "interrupt-word"
    assert _gate("ఆగండి") == "interrupt-word"


def test_an_acknowledgement_is_declined_not_swallowed():
    """Declining suppresses the interruption only; the turn still starts."""
    assert _gate("సరే") == "backchannel"


def test_the_floor_still_governs_when_no_transcript_has_arrived():
    """The usual live case: STT is slower than the interruption decision."""
    reason = _gate("")
    assert reason.startswith("too-short:"), reason
