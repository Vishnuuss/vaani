"""Run 323 -- the booking that was two days and one hour wrong.

WR-TEL-OUT-06749371, 30 August, 100 seconds. A clean qualification: bill, city,
property, roof, name, all six fields inside a minute. Then:

    AGENT   ఉచిత సైట్ సర్వే కోసం మీరు ఏ సమయానికి సిద్ధంగా ఉంటారు?
    CALLER  ఎల్లుండి సాయంత్రం ఐదు ఇంటికి.
    AGENT   మంచిది, సాయంత్రం ఐదు గంటలకు మీ ఇంటికి వచ్చి ...
    CALLER  ఓకే.
    AGENT   సారీ సార్, మీరు చెప్పిన సమయం మా ఎంపికలలో లేదు.
            రేపు ఉదయం ten o'clock లేదా ఎల్లుండి సాయంత్రం four o'clock?
    CALLER  4 ఓ క్లాక్
    AGENT   సరే, ఈ రోజు సాయంత్రం four o'clockకి మీ ఇంటికి వచ్చి ...

Stored: 2026-08-30T16:00. Offered: 2026-09-01T16:00. He chose the second option
and was booked for today. Nobody on that call could have noticed.

Four defects, any one of which alone produces a wrong booking:

1. His time was discarded because no menu had been rendered yet.
2. The agent told him a time he had named was not available.
3. "four", chosen from a menu, resolved against the wall clock, not the menu.
4. The confirmation dropped the day, and welded కి onto o'clock.
"""

from datetime import datetime, timedelta

import pytest

from api.services.vaani import booking, triage
from api.services.vaani.booking import IST, Slot, parse_slot
from api.services.vaani.speech_register import spoken
from api.services.vaani.state import CallState

NOW = datetime(2026, 8, 30, 11, 0, tzinfo=IST)
MENU = (Slot(datetime(2026, 8, 31, 10, 0, tzinfo=IST)),     # రేపు ఉదయం ten
        Slot(datetime(2026, 9, 1, 16, 0, tzinfo=IST)))      # ఎల్లుండి సాయంత్రం four


def booking_state():
    return CallState(required_fields=["assessment_agreed"],
                     questions={"assessment_agreed": "ఒక సమయం పెట్టుకుందామా?"})


# --- 1. a time volunteered before the menu ----------------------------------

def test_a_time_named_before_the_menu_is_not_thrown_away():
    """The exact utterance run 323 discarded."""
    s = booking_state()
    assert s.offered == (), "the menu has not been rendered yet"
    triage.apply(s, "ఎల్లుండి సాయంత్రం ఐదు ఇంటికి.")
    assert s.appointment_iso, (
        "he was asked what time suited him and he answered; that is a booking")
    # Relative to TODAY, not to the day this test was written. The first version
    # asserted the literal 2026-09-01 and passed for exactly one day.
    booked = datetime.fromisoformat(s.appointment_iso)
    assert booked.date() - datetime.now(IST).date() == timedelta(days=2), (
        f"ఎల్లుండి is two days out; got {booked}")
    assert booked.hour == 17, booked


@pytest.mark.parametrize("said", [
    "మూడు లక్షలు",                    # run 266: the answer to the BILL question
    "యాభై వేలు వస్తుంది అండి",
    "ఫైవ్ లాక్స్",
    "ఆ బాగుంటుంది, ఓకే",              # run 262: consent, not a time
])
def test_the_old_gate_still_holds_for_everything_that_is_not_a_time(said):
    """Widening the gate must not reopen run 266."""
    assert not booking.names_a_time_unprompted(said)
    s = booking_state()
    triage.apply(s, said)
    assert s.appointment_iso == "", f"{said!r} booked a visit"


def test_a_day_mentioned_in_passing_is_not_an_appointment():
    """Day after tomorrow my family are going to the village -- names a day and
    asks for nothing. An hour has to be named too."""
    assert not booking.names_a_time_unprompted(
        "ఎల్లుండి మా వాళ్ళు ఊరికి వెళ్తున్నారు")


# --- 2. the menu settles the day --------------------------------------------

@pytest.mark.parametrize("said", ["4 ఓ క్లాక్", "ఫోర్ ఓ క్లాక్", "four o'clock",
                                  "four", "ఫోర్"])
def test_an_hour_chosen_from_the_menu_keeps_the_day_it_was_offered_on(said):
    """The single most damaging line in run 323.

    He answered a two-item menu with the half that distinguishes the items,
    which is how anybody answers a closed question. The dropped half has to
    come back from the menu.
    """
    when = parse_slot(said, NOW, offered=MENU)
    assert when == MENU[1].when, f"{said!r} -> {when}, offered {MENU[1].when}"


def test_the_other_option_still_resolves_to_its_own_day():
    assert parse_slot("ten", NOW, offered=MENU) == MENU[0].when
    assert parse_slot("టెన్", NOW, offered=MENU) == MENU[0].when


@pytest.mark.parametrize("said,index", [
    ("మొదటిది", 0), ("ఫస్ట్ ఒకటి", 0),
    ("రెండోది", 1), ("రెండవది", 1),
])
def test_a_slot_chosen_by_position_is_a_choice(said, index):
    assert parse_slot(said, NOW, offered=MENU) == MENU[index].when


def test_an_hour_matching_both_offers_is_not_a_choice():
    """Two offers at the same hour name nothing. Ask again rather than guess --
    which is the whole lesson of run 262."""
    same = (Slot(datetime(2026, 8, 31, 10, 0, tzinfo=IST)),
            Slot(datetime(2026, 9, 1, 10, 0, tzinfo=IST)))
    assert parse_slot("ten", NOW, offered=same) != same[1].when


def test_no_menu_still_parses_the_way_it_always_did():
    assert parse_slot("రేపు ఉదయం ten o clock", NOW).hour == 10
    assert parse_slot("ఆ బాగుంటుంది, ఓకే", NOW) is None


# --- 3. the agent may not refuse a time the caller names --------------------

def test_the_offer_line_never_tells_a_caller_his_time_is_unavailable():
    s = booking_state()
    line = s.offer_line()
    for slot in s.offered:
        assert slot.say() in line, "both times must be in the words to say"
    assert "ACCEPT any time" in line
    assert "unavailable" in line, "the refusal is named so it cannot be produced"


def test_the_offer_line_forbids_naming_options_without_saying_them():
    """Run 322 asked which of these two suits you, having said neither. The
    caller repeated himself because there was nothing to choose between."""
    assert "the two options" in booking_state().offer_line()


# --- 4. the confirmation carries the day, and the clock is not welded -------

def test_the_booked_line_demands_the_day():
    s = booking_state()
    s.render()
    triage.apply(s, "ఎల్లుండి సాయంత్రం ఐదు ఇంటికి.")
    block = s.render()
    assert "ఎల్లుండి" in block, "a time with no day is not an appointment"
    assert "day included" in block


@pytest.mark.parametrize("said,want", [
    ("ఈ రోజు సాయంత్రం four o'clockకి వస్తాం", "ఈ రోజు సాయంత్రం four o'clock వస్తాం"),
    ("ten o'clockకు వస్తాను", "ten o'clock వస్తాను"),
    ("four oclockకి", "four oclock"),
])
def test_the_telugu_case_marker_comes_off_the_english_clock(said, want):
    """Runs 300, 314, 317 and 323. The register guide names it as the sound of
    a machine, and _hours only ever looked at గంట."""
    assert spoken(said) == want


def test_both_consistent_clocks_are_still_left_alone():
    assert spoken("రేపు ఉదయం పది గంటలకు") == "రేపు ఉదయం పది గంటలకు"
    assert spoken("రేపు ఉదయం ten o'clock") == "రేపు ఉదయం ten o'clock"
    assert spoken("eight to ten గంటలు") == "eight to ten గంటలు"


# --- 5. a far-off slot is a weekday, not a date -----------------------------

def test_a_slot_more_than_two_days_out_is_named_by_its_weekday():
    far = Slot(datetime(2026, 9, 8, 10, 0, tzinfo=IST))
    said = far.say(NOW)
    assert "/" not in said, f"{said!r} reads a date as digits down a phone line"
    assert said.startswith("మంగళవారం"), said


# --- 6. the whole call, end to end ------------------------------------------

def test_run_323_replayed():
    """Every caller utterance from the real call, in order."""
    fields = ["monthly_bill", "location", "property_type", "roof_available",
              "customer_name", "assessment_agreed"]
    s = CallState(required_fields=fields,
                  questions={f: f"[{f}]?" for f in fields})
    for said in ["ఆ మాట్లాడొచ్చు.", "యాభై వేలు వస్తుంది అండి.", "హైదరాబాద్",
                 "ఇల్లు.", "ఆ ఉంది.", "కైలాష్.", "ఆ నిర్ణయించండి.", "ఆ.",
                 "ఎల్లుండి సాయంత్రం ఐదు ఇంటికి.", "ఓకే.", "4 ఓ క్లాక్"]:
        s.last_user_text = said
        triage.apply(s, said)
        s.render()
        s.advance()
    booked = datetime.fromisoformat(s.appointment_iso)
    assert (booked.date() - datetime.now(IST).date()) == timedelta(days=2), (
        f"he said ఎల్లుండి and was booked for {booked}")
    assert booked.hour == 17
