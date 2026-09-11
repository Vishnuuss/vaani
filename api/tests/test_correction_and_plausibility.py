"""A caller may change his mind, and a bill of 62 rupees is not a bill.

Both come from the same complaint: the agent takes whatever fragment it heard
and never revisits it. Vishnu, 2026-08-29:

    "i told 10 lkhs but i remeber it is 20 i can go and recooret it should
     take it like real human"

and, on the same call:

    monthly_bill: 62      <- he had said "60 ... aaa ... 70"
"""

import pytest

from api.services.vaani import extractor
from api.services.vaani.corrections import is_correction
from api.services.vaani.state import CallState


def state() -> CallState:
    s = CallState()
    s.required_fields = ["monthly_bill", "location"]
    s.questions = {"monthly_bill": "బిల్లు ఎంత?", "location": "ఏ ఊరు?"}
    return s


# --- the correction --------------------------------------------------------

def test_a_correction_replaces_the_figure_already_captured():
    s = state()
    assert s.note_amount("పది లక్షలు")
    assert s.known["monthly_bill"] == "1000000"

    assert s.note_amount("సారీ, పది కాదు -- ఇరవై లక్షలు")
    assert s.known["monthly_bill"] == "2000000", "the caller's correction wins"


def test_a_number_said_in_passing_still_does_not_overwrite():
    """Run 266's bug must stay fixed. This is the whole reason the gate exists."""
    s = state()
    assert s.note_amount("పది లక్షలు")
    assert not s.note_amount("మూడు లక్షలు"), (
        "a bare later figure is not a correction and must not overwrite")
    assert s.known["monthly_bill"] == "1000000"


def test_a_correction_with_no_new_figure_unsets_nothing():
    """"కాదు" on its own is a man drawing breath, not a retraction."""
    s = state()
    s.note_amount("పది లక్షలు")
    assert not s.note_amount("కాదు కాదు")
    assert s.known["monthly_bill"] == "1000000"


def test_the_reply_is_told_to_say_the_new_figure_back():
    s = state()
    s.note_amount("పది లక్షలు")
    s.note_amount("actually ఇరవై లక్షలు")
    assert "CORRECTED THEMSELVES" in s.render()


def test_a_correction_is_believed_however_large_it_is():
    """The policy here was REVERSED by the client on 11 September.

    It used to refuse an implausible correction and keep the earlier figure.
    That rule also deleted run 887's industry bill of 70 lakhs and contradicted
    runs 864 and 868 to their faces, both of whom hung up. The instruction is
    now: believe him, never delete it, let sales sanity-check the number.

    The cost is visible right here and is accepted knowingly -- "అరవై కోట్లు",
    60 crores a month, is almost certainly "వేలు" misheard as "కోట్లు", and it
    is now recorded. A figure that is recorded can be corrected later; one that
    was never recorded cannot. The MIN_PLAUSIBLE floor still stands, so a stray
    digit is not a bill -- see test_the_extractor_cannot_store_a_62_rupee_monthly_bill.
    """
    s = state()
    s.note_amount("పది లక్షలు")
    assert s.note_amount("సారీ, అరవై కోట్లు")
    assert s.known["monthly_bill"] == "600000000"


# --- the implausible figure ------------------------------------------------

def test_the_extractor_cannot_store_a_62_rupee_monthly_bill():
    """Exactly what run 295 recorded."""
    s = state()
    extractor.apply_to_state(s, {"monthly_bill": 62}, s.required_fields)
    assert "monthly_bill" not in s.known


def test_a_real_bill_still_goes_straight_in():
    s = state()
    extractor.apply_to_state(s, {"monthly_bill": 100000}, s.required_fields)
    assert s.known["monthly_bill"] == "100000"


def test_a_non_money_field_is_untouched_by_the_check():
    s = state()
    extractor.apply_to_state(s, {"location": "Hyderabad"}, s.required_fields)
    assert s.known["location"] == "Hyderabad"


@pytest.mark.parametrize("text", [
    "సారీ, పది కాదు ఇరవై", "actually twenty lakhs", "no no, twenty",
    "i meant 20 lakhs", "ఇరవై కాదండి పది",
])
def test_real_repairs_are_recognised(text):
    assert is_correction(text)


@pytest.mark.parametrize("text", [
    "పది లక్షలు", "హైదరాబాద్", "సరే", "అవును సార్",
])
def test_ordinary_answers_are_not_repairs(text):
    assert not is_correction(text)
