"""An appointment is a day and a time, or it is not an appointment.

Run 262 is the case this file is built around. The agent offered two slots, the
caller said "ఆ బాగుంటుంది, ఓకే నాకైతే ఓకే" -- which names neither -- and the
agent announced "రేపు ఉదయం ten oclockకి మా వేండర్ వస్తారు" as though it were
settled. The saved record holds `assessment_agreed: true` and no time at all.

Two things have to be true and they are tested separately:

  - agreement and selection are different facts. Saying yes to a menu of two
    is not choosing one, and must never silently become the first.
  - a chosen slot resolves to a real datetime somebody can put in a diary.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from api.services.vaani.booking import (
    FIRST_HOUR,
    IST,
    LAST_HOUR,
    agreed,
    declined,
    offer_slots,
    parse_slot,
)

# A Friday afternoon, so "today" still has room and "tomorrow" is a real day.
NOW = datetime(2026, 8, 28, 15, 0, tzinfo=IST)


# --- the run 262 defect ------------------------------------------------------


def test_agreeing_to_two_slots_does_not_choose_one():
    """The exact words from run 262. This must NOT become a booking."""
    said = "ఆ బాగుంటుంది, ఓకే నాకైతే ఓకే"
    assert agreed(said) is True, "he did consent to a visit"
    assert parse_slot(said, NOW) is None, (
        "he never said which slot -- picking one for him is how a vendor "
        "turns up on the wrong day")


@pytest.mark.parametrize("said", ["సరే", "అలాగే", "ఓకే", "పర్వాలేదు", "okay"])
def test_bare_consent_never_yields_a_time(said):
    assert agreed(said) is True
    assert parse_slot(said, NOW) is None


# --- real choices ------------------------------------------------------------


@pytest.mark.parametrize("said,day,hour", [
    ("రేపు ఉదయం ten o clock", 29, 10),
    ("ఎల్లుండి సాయంత్రం", 30, 17),
    ("మధ్యాహ్నం రెండు గంటలకు", 29, 14),
    ("రేపు", 29, 10),
    ("morning ten", 29, 10),
])
def test_a_named_slot_resolves_to_a_real_datetime(said, day, hour):
    when = parse_slot(said, NOW)
    assert when is not None, f"failed to read a time out of {said!r}"
    assert (when.day, when.hour) == (day, hour)


def test_the_part_of_day_decides_am_or_pm():
    """"మధ్యాహ్నం two" is 14:00. Booking a survey at 2am is not a small slip."""
    assert parse_slot("మధ్యాహ్నం two", NOW).hour == 14
    assert parse_slot("ఉదయం ten", NOW).hour == 10


def test_a_slot_is_never_in_the_past():
    when = parse_slot("ఉదయం ten", NOW)
    assert when > NOW


def test_slots_stay_in_daylight_hours():
    """A roof survey needs light, and nobody wants a 7am call-out."""
    for said in ["రాత్రి", "ఉదయం", "సాయంత్రం", "రేపు", "ఎల్లుండి"]:
        when = parse_slot(said, NOW)
        if when is not None:
            assert FIRST_HOUR <= when.hour < LAST_HOUR, f"{said} -> {when}"


# --- refusal -----------------------------------------------------------------


@pytest.mark.parametrize("said", ["వద్దు", "అవసరం లేదు", "కుదరదు", "not interested"])
def test_a_refusal_is_not_read_as_agreement(said):
    assert declined(said) is True
    assert agreed(said) is False, "reading a refusal as a yes books an unwanted visit"


# --- what gets offered -------------------------------------------------------


def test_two_offers_are_on_different_days():
    """Two times on one day are easy to mishear, and a mishears is a wrong visit."""
    a, b = offer_slots(NOW)
    assert a.when.date() != b.when.date()


def test_offers_are_in_the_future_and_in_hours():
    for slot in offer_slots(NOW):
        assert slot.when > NOW
        assert FIRST_HOUR <= slot.when.hour < LAST_HOUR


def test_an_offer_is_spoken_as_a_concrete_choice():
    """Open questions make the caller invent a format nobody can parse."""
    a, _ = offer_slots(NOW)
    said = a.say(NOW)
    assert "రేపు" in said and "ten" in said, said


def test_an_offer_can_be_read_back():
    """Whatever the agent offers, it must be able to understand if accepted."""
    now = NOW
    for slot in offer_slots(now):
        parsed = parse_slot(slot.say(now), now)
        assert parsed == slot.when, f"{slot.say(now)!r} did not round-trip"


def test_a_late_evening_call_offers_the_next_morning():
    late = NOW.replace(hour=21)
    for slot in offer_slots(late):
        assert FIRST_HOUR <= slot.when.hour < LAST_HOUR
        assert slot.when > late


def test_the_stored_value_is_an_iso_timestamp():
    """A diary entry, not a sentence: this is what a vendor system consumes."""
    a, _ = offer_slots(NOW)
    assert a.iso.startswith("2026-")
    assert datetime.fromisoformat(a.iso) == a.when


def test_offers_are_stable_across_a_call():
    """Re-offering different times mid-call is how a caller loses confidence."""
    assert offer_slots(NOW) == offer_slots(NOW + timedelta(seconds=30))


# --- two customers must never get the same slot ------------------------------
#
# Offering an appointment that is already promised is the worst outcome this
# module can produce: both customers are told a vendor is coming, both wait in,
# and one is stood up. That costs the client the customer, not just the visit.


def test_a_booked_slot_is_never_offered_again():
    first, second = offer_slots(NOW)
    a, b = offer_slots(NOW, taken=[first.iso])
    assert first.when not in (a.when, b.when)


def test_offers_keep_coming_as_slots_fill_up():
    taken, seen = [], set()
    for _ in range(6):
        a, b = offer_slots(NOW, taken=taken)
        assert a.when not in seen and b.when not in seen
        seen.update({a.when, b.when})
        taken += [a.iso, b.iso]
    assert len(seen) == 12, "each round must produce two genuinely new slots"


def test_the_two_offers_differ_in_both_day_and_hour():
    """"రేపు ఉదయం ten" against "ఎల్లుండి ఉదయం ten" is one mishearing apart."""
    for taken in ([], [offer_slots(NOW)[0].iso]):
        a, b = offer_slots(NOW, taken=taken)
        assert a.when.date() != b.when.date()
        assert a.when.hour != b.when.hour


def test_taken_slots_are_accepted_as_iso_strings_or_datetimes():
    """The stored value is ISO; a caller passing datetimes must work too."""
    first, _ = offer_slots(NOW)
    by_iso = offer_slots(NOW, taken=[first.iso])
    by_dt = offer_slots(NOW, taken=[first.when])
    assert [s.when for s in by_iso] == [s.when for s in by_dt]


def test_unparseable_entries_do_not_block_booking():
    """A malformed record must not stop the agent offering anything at all."""
    a, b = offer_slots(NOW, taken=["not a date", None, ""])
    assert a.when > NOW and b.when > NOW


def test_a_full_diary_still_offers_something():
    """A human can move a booking; the agent cannot improvise from nothing."""
    taken = []
    for _ in range(40):
        taken += [s.iso for s in offer_slots(NOW, taken=taken)]
    a, b = offer_slots(NOW, taken=taken)
    assert a.when > NOW and b.when > NOW
