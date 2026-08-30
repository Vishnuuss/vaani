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

import pytest

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
    """The clock word is Telugu; the hour stays English.

    This asserted "oclock" until run 314, where Cartesia read
    "రేపు ఉదయం ten oclock" aloud and it sounded exactly as spliced as it looks
    -- a bare English word dropped into a Telugu sentence, with the Telugu case
    suffix glued onto it ("four oclockకి") whenever a suffix was needed.

    The English NUMBER stays: Telugu callers say the hour in English and the
    client asked for it that way. Only "oclock" moves.
    """
    st = state()
    block = st.render()
    # Both times, in the words to say them in -- asserted against the slots the
    # booking system actually chose rather than against a fixed sentence, so
    # rewording the instruction cannot quietly stop it naming them.
    for slot in st.offered:
        assert slot.say() in block, block
    assert "o'clock" in block, block
    assert "ten" in block, block


def test_the_offer_is_not_a_yes_no_question():
    """A yes/no was answered perfectly in run 262 and still booked nothing."""
    block = state().render()
    assert "A bare yes is not a booking" in block
    assert "ask WHICH" in block


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
    assert "say those exact words back" in block.lower()
    # Run 323 said back the hour with no day on it, which is not an
    # appointment -- it is something the caller and the vendor will remember
    # differently.
    assert "day included" in block


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


# --- run 266: a number is not a time -----------------------------------------
#
# The worst regression this project has shipped. The booking parser ran on every
# caller utterance, so an answer to the BILL question became an appointment:
#
#   AGENT   సరే, మీ నెలవారీ బిల్లు ఎంత రూపాయలుగా ఉంటుంది?
#   CALLER  మూడు లక్షలు                                    (three lakhs)
#   AGENT   రేపు మధ్యాహ్నం three oclock కి మీ సైట్ అసెస్‌మెంట్ బుక్ చేసుకున్నాం.
#
# Stored: monthly_bill 300000, appointment_time 2026-08-29T15:00. The same digit
# read twice. The call ended after 38 seconds, on turn three, having asked
# nothing about location, property, roof or name -- and having promised a vendor
# visit the caller was never offered.


@pytest.mark.parametrize("said", [
    "మూడు లక్షలు",
    "ఫైవ్ లాక్స్",
    "10 లక్షల రూపాయలకు",
    "300000 rupees",
    "టెన్ టు ట్వంటీ లాక్స్",
    "రెండు వేలు",
])
def test_an_amount_is_never_read_as_a_time(said):
    from api.services.vaani.booking import parse_slot
    assert parse_slot(said) is None, f"{said!r} is money, not a clock"


def test_nothing_books_before_a_slot_has_been_offered():
    """Run 266 replayed. On turn three there is no question a time answers."""
    st = CallState(
        required_fields=["monthly_bill", "location", "assessment_agreed"],
        questions={"monthly_bill": "బిల్లు ఎంత?", "location": "ఏ ప్రాంతం?",
                   "assessment_agreed": "షెడ్యూల్ చేయాలా?"},
    )
    st.render()                      # asking the BILL, not for a time
    triage.apply(st, "మూడు లక్షలు")
    assert st.appointment_iso == ""
    assert st.offered == (), "no slots were offered, so none can be chosen"


def test_a_time_still_books_once_slots_are_on_the_table():
    """The gate must not break real booking."""
    st = state()
    st.render()                      # offers two slots
    assert st.offered, "the booking field must offer slots"
    triage.apply(st, "రేపు ఉదయం ten o clock")
    assert st.appointment_iso
