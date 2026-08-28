"""The agent must not answer its own voice.

Run 270 is 31 seconds long and contains no human content whatsoever:

    AGENT   సరే, మీ పేరు,        CALLER  సరే, మీ పేరు?
    AGENT   మీ నెల బిల్లు ఎంత     CALLER  మీ నెల బిల్లు
    AGENT   మంచిది, మీ           CALLER  మంచిది.
    AGENT   సరే, మీరు ఏ          CALLER  సరే మీరు ఏ

Every "caller" line is the sentence the agent had just spoken, coming back off a
speakerphone. Run 261 was the same and was proved acoustically: the caller track
matched the agent track delayed 300ms, correlating 0.88 on the envelope.

The loop runs away on its own -- speak, hear yourself, treat it as an
interruption, abandon the sentence, answer yourself, hear that too -- which is
why run 270 degenerated into "మంచిది / మంచిది / సరే / సరే" and never reached one
real question. It reads exactly like the complaint: a robot firing questions
without listening.

Telling the client not to use speakerphone is not a fix, because customers will.

The two directions are not equally costly, and the tests are weighted that way:
missing an echo costs one confused turn, while silencing a real caller costs the
call. Every genuine answer below must pass through untouched.
"""

from __future__ import annotations

import pytest

from api.services.vaani.state import echoes_agent

# (what the agent said, what came back) -- verbatim from run 270.
ECHOES = [
    ("సరే, మీ పేరు,", "సరే, మీ పేరు?"),
    ("మీ నెల బిల్లు ఎంత", "మీ నెల బిల్లు"),
    ("మంచిది, మీ", "మంచిది."),
    ("సరే, మీరు ఏ", "సరే మీరు ఏ"),
    ("మీకు మీ స్వంత", "మీకు మీ సూచి"),      # the STT mishears the tail
    ("రేపు ఉదయం ten", "రేపు ఉదయం"),
]

# Verbatim from run 269, a real conversation. None may be suppressed.
REAL = [
    ("సరే, మీ నెలవారీ బిల్లు ఎంత rupees ఉంటుందో చెప్పగలరా?", "మాది పది లక్షలు అండి."),
    ("మీరు ఏ నగరంలో లేదా ప్రాంతంలో ఉన్నారు?", "మేము హైదరాబాద్ అండి."),
    ("మీ పేరు చెప్పగలరా?", "నా పేరు విశ్వా"),
    ("ఫ్యాక్టరీకి మీకు స్వంతంగా రూఫ్ స్పేస్ ఉందా?", "ఉందండి."),
    ("సరే, మీరు పర్సనల్ లేదా ఫ్యాక్టరీ ఏది?", "ఫ్యాక్టరీ అండి ఫ్యాక్టరీ"),
    ("ఏ డౌట్స్ ఉన్నాయో చెప్పగలరా?",
     "మాకు ఇప్పుడు సబ్సిడీ వస్తుందా సోలార్ నుంచి సబ్సిడీ వస్తుందా?"),
    ("మీ ఫ్యాక్టరీకి సైట్ అసెస్‌మెంట్ కోసం ఏ సమయం బాగుంటుంది?",
     "మాకు ఈరోజే అండి ఇప్పుడు ఇప్పుడు బాగుంటది"),
]


@pytest.mark.parametrize("said,heard", ECHOES)
def test_the_agents_own_words_are_recognised(said, heard):
    assert echoes_agent(heard, [said]) is True


@pytest.mark.parametrize("said,heard", REAL)
def test_a_real_answer_is_never_suppressed(said, heard):
    """The costly direction. Silencing a caller loses the call outright."""
    assert echoes_agent(heard, [said]) is False, (
        f"{heard!r} is a real answer to {said!r}")


def test_a_short_fragment_is_left_alone():
    """"ఆ" is both an echo fragment and a real Telugu backchannel.

    There is no way to tell them apart from text, so it is treated as the
    caller: hearing an echo costs a turn, silencing a caller costs the call.
    """
    assert echoes_agent("ఆ", ["ఆ, సరే"]) is False
    assert echoes_agent("ఆ.", ["మంచిది, మీరు ఏ ప్రాంతం?"]) is False


def test_only_the_last_few_utterances_are_compared():
    """An echo is immediate. Matching against the whole call would eventually
    suppress a caller who legitimately repeats a word the agent used earlier."""
    old = ["మీ పేరు చెప్పగలరా?", "a", "b", "c"]
    assert echoes_agent("మీ పేరు చెప్పగలరా?", old) is False


def test_nothing_said_yet_suppresses_nothing():
    assert echoes_agent("మాది పది లక్షలు అండి.", []) is False


def test_the_run_270_call_would_have_been_stopped():
    """End to end: how much of that call was the agent talking to itself."""
    caught = sum(echoes_agent(heard, [said]) for said, heard in ECHOES)
    assert caught >= 5, f"only {caught}/{len(ECHOES)} echo turns recognised"
