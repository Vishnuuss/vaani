"""The booking exchange from run 262, end to end.

That call is the reason this exists. The agent offered two slots, the caller
said "ఆ బాగుంటుంది, ఓకే నాకైతే ఓకే", and the agent replied "రేపు ఉదయం ten
oclockకి మా వేండర్ వస్తారు" -- announcing a time the caller had not chosen. The
record saved for that call was `assessment_agreed: true`, with no day and no
time in it anywhere.

So two things are checked here against the live state block, not just the
parser: that agreeing without naming a time does not produce a booking, and
that naming one does produce a timestamp somebody can put in a diary.
"""

from __future__ import annotations

from datetime import datetime

from api.services.vaani import triage
from api.services.vaani.booking import IST
from api.services.vaani.state import CallState


def state() -> CallState:
    """The real workflow's last field: agreeing a site assessment."""
    return CallState(
        required_fields=["assessment_agreed"],
        questions={"assessment_agreed": "సైట్ అసెస్‌మెంట్ షెడ్యూల్ చేయాలా?"},
    )


def test_the_offer_names_two_real_times():
    block = state().render()
    assert "OFFER EXACTLY THESE TWO TIMES" in block
    assert "oclock" in block, block


def test_the_offer_is_not_a_yes_no_question():
    """A yes/no was answered perfectly in run 262 and still booked nothing."""
    block = state().render()
    assert "THEY MUST NAME WHICH ONE" in block


def test_the_two_offers_do_not_change_between_turns():
    """Re-offering different times mid-call is how a caller loses confidence."""
    st = state()
    first = st.render()
    assert st.render() == first.replace("TURN: 0", "TURN: 0")


def test_agreeing_without_naming_a_time_books_nothing():
    """The exact utterance from run 262."""
    st = state()
    st.render()
    triage.apply(st, "ఆ బాగుంటుంది, ఓకే నాకైతే ఓకే")
    assert st.appointment_iso == "", (
        "consent to meet is not a time; booking one anyway is how a vendor "
        "arrives on the wrong day")


def test_naming_a_time_books_it():
    st = state()
    st.render()
    triage.apply(st, "రేపు ఉదయం ten o clock వస్తే బాగుంటుంది")
    assert st.appointment_iso, "a named slot must be recorded"
    when = datetime.fromisoformat(st.appointment_iso)
    assert when.hour == 10
    assert when > datetime.now(IST)


def test_a_booking_stops_the_call_rather_than_selling_on():
    st = state()
    st.render()
    triage.apply(st, "రేపు ఉదయం ten o clock")
    block = st.render()
    assert "BOOKED" in block
    assert "END THE CALL" in block
    assert "STILL_NEED: []" in block


def test_the_booked_time_is_read_back_for_correction():
    """The caller is the only one who can catch a misheard slot."""
    st = state()
    st.render()
    triage.apply(st, "ఎల్లుండి సాయంత్రం")
    block = st.render()
    assert "say that time back" in block.lower()


def test_a_refusal_does_not_book_anything():
    st = state()
    st.render()
    triage.apply(st, "వద్దు అండి, అవసరం లేదు")
    assert st.appointment_iso == ""


def test_a_booking_is_not_overwritten_by_later_chatter():
    """Once agreed, a passing "రేపు" in conversation must not move the visit."""
    st = state()
    st.render()
    triage.apply(st, "రేపు ఉదయం ten o clock")
    booked = st.appointment_iso
    triage.apply(st, "ఎల్లుండి మా వాళ్ళు ఊరికి వెళ్తున్నారు")
    assert st.appointment_iso == booked


def test_a_non_booking_field_still_gets_its_normal_question():
    """The booking treatment must not leak into ordinary qualification."""
    st = CallState(required_fields=["monthly_bill"],
                   questions={"monthly_bill": "బిల్లు ఎంత?"})
    block = st.render()
    assert "NEXT QUESTION TO ASK" in block
    assert "OFFER EXACTLY THESE TWO TIMES" not in block
