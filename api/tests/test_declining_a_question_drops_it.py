"""A caller who declines a question must not be asked it again.

The bug
-------
Telugu marks "did not" and "cannot" one character apart:

    చెప్పలేదు   past negative      "I did NOT say it"   -> you skipped me
    చెప్పలేను   ability negative   "I CANNOT say it"    -> a refusal

`NOT_YET_ANSWERED` listed `లేను` inside its alternation, so both readings hit
the same branch -- and that branch calls `_refund_ask`, which puts the field
back into `still_need` and hands back the ask that was spent on it.

A refusal therefore RESET the two-ask budget instead of ending it. The field
could never age out, and the agent asked the same thing indefinitely. Run 844:
the caller said "నేను చెప్పలేను" and the bill was asked four times, twice after
he had declined it. The client's words: *"if customer don't want to answer, skip
the answer ... looping means at least change the sentences"*.

Both halves matter and they pull in opposite directions, which is why one regex
could never serve both: a caller complaining he was skipped needs the question
BACK, and a caller declining it needs it GONE.
"""

from __future__ import annotations

import pytest

from api.services.vaani.triage import CANNOT_ANSWER, NOT_YET_ANSWERED


DECLINES = [
    "నేను చెప్పలేను",
    "చెప్పలేను అండి",
    "తెలియదు",
    "గుర్తు లేదు",
    "అది చెప్పను",
    "i can't say",
    "i cannot tell",
    "i won't say",
    "i don't know",
]

COMPLAINS_HE_WAS_SKIPPED = [
    "నేను బిల్లు చెప్పలేదు",
    "నేను చెప్పలే కదా ఎందుకు ముందుకు పోతున్నావ్",
    "ఏం బిల్ చెప్పలా",
    "అసలు ఏం చెప్పలేదు",
    "you skipped my answer",
    "i didn't tell you",
]

NEITHER = ["రెండు వేలు", "విజయవాడ", "సొంత ఇల్లే", "సరే", "i can say"]


@pytest.mark.parametrize("text", DECLINES)
def test_a_refusal_is_recognised_as_a_refusal(text):
    assert CANNOT_ANSWER.search(text), (
        f"{text!r} is a caller declining a question. Unrecognised, the field "
        "stays in still_need and he is asked again."
    )


@pytest.mark.parametrize("text", DECLINES)
def test_a_refusal_is_not_read_as_being_skipped(text):
    """The half that caused the loop.

    Matching NOT_YET_ANSWERED here refunds the ask, which is the OPPOSITE of
    what a refusal should do. `CANNOT_ANSWER` is tested first in `triage`, so a
    Telugu form that legitimately matches both is still handled correctly -- but
    a form that ONLY matches NOT_YET_ANSWERED would be refunded, so those are
    the ones this catches.
    """
    if NOT_YET_ANSWERED.search(text):
        assert CANNOT_ANSWER.search(text), (
            f"{text!r} matches only NOT_YET_ANSWERED, so it will be refunded "
            "and asked again -- the opposite of skipping it."
        )


@pytest.mark.parametrize("text", COMPLAINS_HE_WAS_SKIPPED)
def test_being_skipped_still_refunds(text):
    """The behaviour that must NOT regress.

    Run 804's caller said this three times while the field he wanted to answer
    sat abandoned at its cap. Refunding is the only thing that lets the agent
    ask it again, and it is worth more than the fix above.
    """
    assert NOT_YET_ANSWERED.search(text), f"{text!r} must still refund the ask"
    assert not CANNOT_ANSWER.search(text), (
        f"{text!r} is a complaint about being skipped, not a refusal. Reading "
        "it as a refusal would drop the question he is asking to answer."
    )


@pytest.mark.parametrize("text", NEITHER)
def test_a_plain_answer_triggers_neither(text):
    assert not CANNOT_ANSWER.search(text)
    assert not NOT_YET_ANSWERED.search(text)


def test_the_one_character_that_separates_them():
    """లేదు against లేను, which is the whole bug in one assertion."""
    did_not = "నేను చెప్పలేదు"      # I did not say it
    cannot = "నేను చెప్పలేను"       # I cannot say it
    assert NOT_YET_ANSWERED.search(did_not) and not CANNOT_ANSWER.search(did_not)
    assert CANNOT_ANSWER.search(cannot)


def test_abandon_falls_back_to_the_head_of_the_checklist():
    """`pending_ask` is routinely empty by the time a refusal is triaged.

    It is cleared as soon as a reply is produced, and `last_asked` with it, so
    an abandon that depends on either does nothing at all -- silently. Run 845
    is that bug: the caller said "నేను చెప్పలేను" and was asked the bill twice
    more, with the fix deployed.
    """
    from api.services.vaani.triage import _abandon_ask

    class S:
        MAX_ASKS_PER_FIELD = 2
        pending_ask = ""
        last_asked = ""
        still_need = ["monthly_bill", "location"]
        ask_counts = {}

    s = S()
    _abandon_ask(s, "declined")
    assert s.ask_counts.get("monthly_bill") == 2, (
        "with pending_ask empty the head of still_need is the field being "
        "answered; without this fallback the refusal is silently ignored"
    )
    assert s.ask_counts.get("location") is None, "only the current field"


def test_abandon_spends_the_whole_budget():
    """`_abandon_ask` must push the field out of `still_need`, not nudge it."""
    from api.services.vaani.triage import _abandon_ask

    class S:
        MAX_ASKS_PER_FIELD = 2
        pending_ask = "monthly_bill"
        last_asked = "monthly_bill"
        ask_counts = {"monthly_bill": 1}

    s = S()
    _abandon_ask(s, "test")
    assert s.ask_counts["monthly_bill"] >= S.MAX_ASKS_PER_FIELD, (
        "the field must reach its cap, which is how it leaves the checklist"
    )
    assert s.last_asked == ""
