"""Run 882 (11 Sep): he asked where we were calling from and got a goodbye.

    USER  పేయించుకుంటాను కానీ... కాస్ట్ ఎంత వేసింది మామూలుగా?
    BOT   [deflects, asks for a time]
    USER  సరే. మీరు ఎక్కడి నుంచి?              where are you calling from?
    BOT   పర్వాలేదు అండి. మీకు కావాలంటే ఎప్పుడైనా కాల్ చేయండి. థాంక్యూ.

`_end_is_earned` already refuses MODE: END on an interested caller -- that is
run 880's fix, shipped the same day. It did not catch this one, because by then
he HAD agreed to the visit, so `next_step_agreed` made the ending "earned" and
the request was granted while he was in the middle of a question.

Agreeing to a site survey is not agreeing to stop talking. A question on the
line is a caller still engaged, and hanging up on one is the most expensive
mistake this agent makes.

`must_end` still overrides: a caller who asked to hang up is let go.
"""

import pytest

from api.services.vaani.brain_processor import _end_is_earned
from api.services.vaani.state import CallState


def _state(**kw) -> CallState:
    st = CallState(required_fields=["monthly_bill"],
                   questions={"monthly_bill": "మీ నెలవారీ బిల్లు ఎంత?"})
    for k, v in kw.items():
        setattr(st, k, v)
    return st


@pytest.mark.parametrize("flag,value", [
    ("next_step_agreed", True),
    ("appointment_iso", "2026-09-12T10:00:00+05:30"),
    ("no_more_questions", True),
    ("refusals", 1),
])
def test_run_882_a_question_on_the_line_is_not_an_earned_ending(flag, value):
    st = _state(last_user_text="మీరు ఎక్కడి నుంచి?", **{flag: value})
    assert not _end_is_earned(st), f"{flag} granted a hangup mid-question"


def test_a_caller_who_asked_to_hang_up_is_still_let_go():
    st = _state(last_user_text="మీరు ఎక్కడి నుంచి?", must_end=True)
    assert _end_is_earned(st)


def test_an_ordinary_earned_ending_is_untouched():
    """He agreed and asked nothing. Run 880's fix must keep working as before."""
    assert _end_is_earned(_state(last_user_text="సరే", next_step_agreed=True))
    assert not _end_is_earned(_state(last_user_text="సరే"))
