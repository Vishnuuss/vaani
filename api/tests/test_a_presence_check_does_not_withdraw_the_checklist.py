"""Run 982 (18 Sep): he answered, and the answer was thrown away.

    BOT   ... మీకు పిల్లలు ఉన్నారా అండి
    USER  నాకు పిల్లలు లేరు.                        I have no children.
          హలో? నేను ఇంకా కాల్‌లో ఉన్నారా?            hello? am I still on the call?
    BOT   అవును సార్, ఇంకా కాల్‌లోనే ఉన్నాం అండి.     yes sir, we are still on the call
          మీరు ఇప్పుడు monthlyగా ఏమైనా investment చేస్తున్నారా అండి?
    USER  నేను చెప్పింది మీకు వినిపించిందా? మాకు పిల్లలు లేరు.
          "did you HEAR what I said? we have no children."

`note_user_said` accumulates a turn, so `last_user_text` was the whole thing --
the answer AND the "hello". `render()` computes `he_asked = _is_question(...)`,
which the "హలో?" makes True, and the `elif he_asked` branch withdraws the
checklist entirely so the agent answers the question instead of qualifying.

That is right for a question and wrong for this. There was no question. The
agent spent its turn confirming it was still on the call, said nothing about the
children, and the caller had to repeat himself to be heard -- which is the
client's complaint in his own words: "it is not answering, it sounds scripted".

Same root cause as the call that would not hang up (see
`test_a_presence_check_is_not_an_engaged_caller`): a presence check carries an
interrogative and is not a question. Here it costs a turn; there it cost 80
seconds of dead air.
"""

import pytest

from api.services.vaani.state import CallState


def _state() -> CallState:
    return CallState(
        required_fields=["currently_investing", "investment_type"],
        questions={"currently_investing": "మీరు monthlyగా investment చేస్తున్నారా?",
                   "investment_type": "దేనిలో చేస్తున్నారు అండి?"})


PRESENCE_TURNS = [
    "హలో?",
    "హలో? నేను ఇంకా కాల్‌లో ఉన్నారా?",
    "నాకు పిల్లలు లేరు. హలో? నేను ఇంకా కాల్‌లో ఉన్నారా?",   # run 982 verbatim
    "hello? are you there?",
]


@pytest.mark.parametrize("text", PRESENCE_TURNS)
def test_a_presence_check_does_not_suppress_the_checklist(text):
    s = _state()
    s.last_user_text = text
    block = s.render()
    assert "STILL_NEED: []" not in block, (
        f"the checklist was withdrawn for a presence check: {text}\n{block}")
    assert "currently_investing" in block


@pytest.mark.parametrize("text", [
    "సబ్సిడీ ఎంత వస్తది?",
    "ఏం చెప్తారు మీ అడ్వైజర్?",
    "హలో, సబ్సిడీ ఎంత వస్తది?",     # a greeting in front of a real question
])
def test_a_real_question_still_suppresses_the_checklist(text):
    """Runs 218/872/882's fix must not regress -- this is the expensive one."""
    s = _state()
    s.last_user_text = text
    block = s.render()
    assert "STILL_NEED: []" in block, f"a real question was not answered first: {text}"
