"""Zero is an answer to the bill question, and it must retire the field.

Run 939 (13 Sep, 12:37 IST) is the case. The caller was asked his monthly
current bill and said "జీరో". `parse_amount` has no zero in it at all -- the
bare-figure branch only accepts a figure of 500 or more, and no scale word
maps a zero -- so it returned None, `note_amount` stored nothing, and
`monthly_bill` stayed in `still_need`.

Two things followed from that one None:

  - the agent asked the bill a second time, word for word, three turns later
  - `monthly_bill` was the ONE field absent from the lead record. Not null --
    absent. Every other field on the checklist came back, including the two
    that were genuinely never answered.

A caller who says zero has answered. He may mean he has no connection, that
the meter is in someone else's name, or that the bus stand he is standing in
is not his to pay for -- all of which are worth more to a vendor than a blank,
and none of which are improved by asking him again.

The 500 floor stays where it is. It exists so a stray "2" in a sentence is not
read as two rupees, and an EXPLICIT zero is not a stray numeral -- it is the
whole of the answer.
"""

from __future__ import annotations

import pytest

from api.services.vaani import amounts


@pytest.mark.parametrize("said", [
    "జీరో",                    # run 939, exactly as Sarvam returned it
    "సున్నా",                   # the Telugu word
    "zero",
    "0",
    "బిల్లు జీరో అండి",
    "నాకు బిల్లు రాదు, జీరో",
])
def test_an_explicit_zero_parses_as_zero(said):
    got = amounts.parse_amount(said)
    assert got is not None, f"{said!r} produced no amount -- the field re-asks"
    assert got.rupees == 0


@pytest.mark.parametrize("said", [
    "రేపు పది గంటలకు",          # a time, not money
    "రెండు కిలోవాట్",            # a system size
    "సరే అండి",                 # no figure at all
])
def test_a_sentence_with_no_zero_in_it_is_still_none(said):
    assert amounts.parse_amount(said) is None


def test_a_stray_small_numeral_is_still_not_an_amount():
    """The 500 floor is untouched by the zero case."""
    assert amounts.parse_amount("రెండు") is None


def test_zero_is_recorded_and_retires_the_field():
    """The point of the parse: `known` gets filled, so `still_need` drops it."""
    from api.services.vaani.state import CallState

    state = CallState()
    state.required_fields = ["monthly_bill", "location"]
    state.questions = {"monthly_bill": "మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?"}

    assert "monthly_bill" in state.still_need
    assert state.note_amount("జీరో") is True
    assert state.known["monthly_bill"] == "0"
    assert "monthly_bill" not in state.still_need
