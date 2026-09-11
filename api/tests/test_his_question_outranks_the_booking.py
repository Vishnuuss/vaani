"""Runs 872, 882 and 885 (11 Sep): he asked the price and was handed a time slot.

    USER  ...సోలార్ ఛానల్స్ కాస్ట్ ఎంత అవుతుంది 60 ఇంటూ 60 స్క్వేర్ ఫీట్ కి?
    BOT   సరే నితేష్ గారు, రేపు ఉదయం ten o'clock ... బుక్ చేశాను. థాంక్యూ అండి.
    USER  ఆ నా క్వశ్చన్ కి ఆన్సర్ ఇవ్వండి               ANSWER MY QUESTION
    BOT   ఖర్చు మీ కెపాసిటీ ... మీద ఆధారపడి ఉంటుంది. రేపు ఉదయం ten o'clock
          లేదా ఎల్లుండి సాయంత్రం four o'clock బుక్ చేశాను. థాంక్యూ అండి.
    USER  అరే బాయ్.                                    [hangs up]

`render()` DOES carry an answer-first instruction -- "THE CALLER ASKED YOU
SOMETHING. FIRST answer THAT, properly." It is simply unreachable here. It sits
at the END of the branch chain, below `must_end`, `disqualified`,
`next_step_agreed`, `buying_signal` and `appointment_iso`. Once the caller has
agreed to a visit, every question he asks for the rest of the call is answered
by a booking line, because the chain never reaches the branch that would have
told the model to answer him.

Run 882 is the same shape: he asked the cost and was asked for a time. Run 872
too, twice.

`must_end` still outranks everything -- a caller who has asked to hang up is not
kept on the line to be answered.
"""

import pytest

from api.services.vaani.state import CallState

COST = "సోలార్ ప్యానెల్స్ కాస్ట్ ఎంత అవుతుంది?"


def _state(**kw) -> CallState:
    st = CallState(required_fields=["monthly_bill", "location"],
                   questions={"monthly_bill": "మీ నెలవారీ బిల్లు ఎంత?",
                              "location": "మీరు ఏ ఊరు?"})
    for k, v in kw.items():
        setattr(st, k, v)
    return st


@pytest.mark.parametrize("flag,value", [
    ("next_step_agreed", True),
    ("buying_signal", True),
    ("disqualified", True),
])
def test_his_question_is_answered_even_once_the_call_is_closing(flag, value):
    block = _state(last_user_text=COST, **{flag: value}).render()
    assert "ASKED YOU SOMETHING" in block, (
        f"{flag} silently outranked the caller's question")
    assert "FIRST answer" in block


def test_a_booked_appointment_keeps_its_own_stronger_answer_first_rule():
    """Not this branch's job: `appointment_iso` already answers him first and
    quotes his exact words in. Gating it would have been a downgrade -- which
    the first version of this change did, and test_rebooking caught."""
    block = _state(last_user_text=COST,
                   appointment_iso="2026-09-12T10:00:00+05:30").render()
    assert "THAT first" in block
    assert COST in block


def test_a_caller_who_asked_to_hang_up_is_still_let_go():
    """`must_end` outranks everything. Run 803: seven closings to a man leaving."""
    block = _state(last_user_text=COST, must_end=True,
                   end_reason="He asked to end the call.").render()
    assert "STOP." in block
    assert "ASKED YOU SOMETHING" not in block


def test_nothing_changes_when_he_simply_answered():
    """A plain answer must still reach the ordinary branches, untouched."""
    block = _state(last_user_text="రెండు వేలు వస్తుంది",
                   next_step_agreed=True).render()
    assert "ASKED YOU SOMETHING" not in block
    assert "THEY AGREED TO THE VISIT" in block
