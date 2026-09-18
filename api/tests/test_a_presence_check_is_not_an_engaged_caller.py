"""Runs 981 and 982 (18 Sep): the call that would not hang up.

Run 982, an investment call. The business was finished -- the advisor call was
agreed for ten the next morning and he said so:

    BOT   మా advisor tomorrow ఉదయం ten o'clock మీకు కాల్ చేస్తారు, సరే అండి?
    USER  సరే.
    BOT   థాంక్యూ సార్ ... థాంక్యూ సార్.
    USER  అలాగేనండి. బాయ్.
    USER  హలో? హలో, కాల్‌లో ఉన్నారా ఇంకా?
    USER  హలో?
    USER  హలో, అక్కా, కాల్‌లో ఉన్నావా?
    BOT   వీడ్కోలు సార్.                        <- 30+ seconds later

171 seconds for a call whose business was done at about 90. Run 981 is the same
tail: the closing line four times, then "బై", then "హలో".

`_end_is_earned` refuses MODE: END whenever `_is_question(last_user_text)`,
which is run 882's fix and is right: a caller with a question on the line is
engaged, and hanging up on him is the most expensive mistake this agent makes.

But "కాల్‌లో ఉన్నారా ఇంకా?" -- *are you still on the call?* -- carries the
interrogative clitic, so it reads as a question, and it is not one. It is a
PRESENCE CHECK: the noise a caller makes because nobody has spoken. It does not
ask for anything and there is nothing to answer.

The direction of the failure is what makes it expensive. Every "are you there?"
is itself proof the agent has stopped talking, and every one of them renews the
refusal to hang up -- so the longer the dead air runs, the more certainly it
cannot end. The caller has to put the phone down himself.

`note_user_said` accumulates a turn, so the real `last_user_text` is the whole
tail concatenated; the check has to survive that, not just the clean sentence.
"""

import pytest

from api.services.vaani.brain_processor import _end_is_earned
from api.services.vaani.state import CallState, _is_presence_check, _is_question


def _state(**kw) -> CallState:
    st = CallState(required_fields=["interested"],
                   questions={"interested": "మీకు ఆసక్తి ఉందా?"})
    for k, v in kw.items():
        setattr(st, k, v)
    return st


PRESENCE = [
    "హలో?",
    "హలో, కాల్‌లో ఉన్నారా ఇంకా?",
    "హలో, అక్కా, కాల్‌లో ఉన్నావా?",
    "నేను ఇంకా కాల్‌లో ఉన్నారా?",
    "హలో? వినిపిస్తుందా?",
    "hello? are you there?",
    "are you still on the line?",
    "can you hear me?",
    # the accumulated turn, which is what actually reaches `last_user_text`
    "అలాగేనండి. బాయ్. హలో? హలో, కాల్‌లో ఉన్నారా ఇంకా? హలో?",
]

REAL_QUESTIONS = [
    "మీరు ఎక్కడి నుంచి?",                       # run 882, must never regress
    "సబ్సిడీ ఎంత వస్తది?",                        # run 983
    "రెసిడెన్షియల్ ప్లాట్ అంటే ఏంటి?",              # run 981
    "ఏం చెప్తారు మీ అడ్వైజర్?",                    # run 982
    "మీ దగ్గర ఏమేం లోన్స్ ఉన్నాయి?",               # run 985
    "హలో, సబ్సిడీ ఎంత వస్తది?",                   # a greeting in FRONT of a real question
]


@pytest.mark.parametrize("text", PRESENCE)
def test_a_presence_check_is_recognised(text):
    assert _is_presence_check(text), f"not recognised as a presence check: {text}"


@pytest.mark.parametrize("text", REAL_QUESTIONS)
def test_a_real_question_is_never_a_presence_check(text):
    assert not _is_presence_check(text), f"a real question was dismissed: {text}"
    assert _is_question(text)


@pytest.mark.parametrize("text", PRESENCE)
@pytest.mark.parametrize("flag,value", [
    ("next_step_agreed", True),
    ("appointment_iso", "2026-09-19T10:00:00+05:30"),
    ("no_more_questions", True),
    ("refusals", 1),
])
def test_run_982_a_presence_check_does_not_block_an_earned_ending(text, flag, value):
    st = _state(last_user_text=text, **{flag: value})
    assert _end_is_earned(st), (
        f"{flag} could not end the call because of a presence check: {text}")


@pytest.mark.parametrize("text", REAL_QUESTIONS)
def test_run_882_stays_fixed(text):
    """A real question still refuses the hangup, however earned the ending."""
    st = _state(last_user_text=text, next_step_agreed=True,
                appointment_iso="2026-09-19T10:00:00+05:30")
    assert not _end_is_earned(st), f"hung up mid-question: {text}"


def test_a_presence_check_cannot_earn_an_ending_on_its_own():
    """It removes a BLOCK; it is not itself a reason to hang up.

    Nothing agreed, nothing refused, a field still unasked -- the caller saying
    "hello?" must not become permission to end the call.
    """
    assert not _end_is_earned(_state(last_user_text="హలో?"))
