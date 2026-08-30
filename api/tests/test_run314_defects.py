"""Run 314: "Mr. Um Bhaskar", and a warm lead pushed until he cooled.

WR-TEL-OUT-70031484. The caller gave his bill, his roof, his name, and asked
two real questions about the company -- a good lead by any measure. Two things
ruined it, and neither was latency.

    BOT  : మంచిది, మీ పేరు చెప్పగలరా?
    USER : ఉమ్ భాస్కర్.                          <- "um... Bhaskar"
    BOT  : మంచిది, ఉమ్ భాస్కర్ గారు. ...          <- "Mr. Um Bhaskar"

and then, at the close:

    USER : ఆ చెప్తాం మేము మళ్ళీ చెప్తాం.          we'll tell you, we'll tell you again
    BOT  : రేపు ఉదయం ten oclock లేదా ... ఏ సమయం మీకు బాగుంటుంది?
    USER : మేము చెప్తాం ఆలోచన చెప్తాం.            we'll think and tell you
    BOT  : రేపు ఉదయం ten oclock లేదా ... దయచేసి ... చెప్పండి.   <- again, plus "please"
    USER : అంటే మేము ఆలోచించి చెప్తాం ... డిసైడ్ అవ్వలేం ఇంకా
"""

import pytest

from api.services.vaani.booking import Slot
from api.services.vaani.completeness import strip_fillers
from api.services.vaani.extractor import apply_to_state
from api.services.vaani.speech_register import spoken
from api.services.vaani.state import CallState
from api.services.vaani.triage import apply as triage_apply
from api.services.vaani.triage import triage

FIELDS = ["monthly_bill", "location", "property_type", "roof_available",
          "customer_name"]

# The three deferrals, verbatim from the run log.
DEFERRALS = [
    "ఆ చెప్తాం మేము మళ్ళీ చెప్తాం.",
    "మేము చెప్తాం ఆలోచన చెప్తాం.",
    "అంటే మేము ఆలోచించి చెప్తాం దాని గురించి డిసైడ్ అవ్వలేం ఇంకా",
]


# --- "um" is not part of anybody's name -------------------------------------

def test_the_name_that_shipped():
    """`customer_name: "ఉమ్ భాస్కర్"` -- the agent said it out loud, twice."""
    assert strip_fillers("ఉమ్ భాస్కర్.") == "భాస్కర్"


def test_the_extractor_stores_the_name_without_the_filler():
    s = CallState()
    apply_to_state(s, {"customer_name": "ఉమ్ భాస్కర్."}, FIELDS, "ఉమ్ భాస్కర్.")
    assert s.known["customer_name"] == "భాస్కర్"


def test_uma_is_a_real_name_and_survives():
    """"ఉమ్" carries a virama and is not a Telugu word. "ఉమ"/"ఉమా" is a name,
    and stripping it would be the same bug pointed the other way."""
    assert strip_fillers("ఉమా గారు") == "ఉమా గారు"
    assert strip_fillers("ఉమ భాస్కర్") == "ఉమ భాస్కర్"


def test_a_filler_inside_a_phrase_is_left_alone():
    """Edges only. Cutting the middle splices two halves of a sentence into
    something the caller never said."""
    assert strip_fillers("రామ్ ఉమ్ కుమార్") == "రామ్ ఉమ్ కుమార్"


def test_a_bare_filler_is_not_erased():
    """An empty name is worse than a wrong one -- nothing downstream can see
    that it happened."""
    assert strip_fillers("ఉమ్") == "ఉమ్"


@pytest.mark.parametrize("said,want", [
    ("um Bhaskar", "Bhaskar"),
    ("ఆ ఉంది", "ఉంది"),
    ("uhh మంచిర్యాల్", "మంచిర్యాల్"),
    ("హైదరాబాద్ ఆ", "హైదరాబాద్"),
])
def test_fillers_at_either_edge(said, want):
    assert strip_fillers(said) == want


# --- a deferral is not a refusal, and it ends the close ---------------------

@pytest.mark.parametrize("said", DEFERRALS)
def test_all_three_deferrals_are_recognised(said):
    assert triage(said).deferred


@pytest.mark.parametrize("said", DEFERRALS)
def test_a_deferral_after_slots_were_offered_ends_the_call(said):
    """The fix. One deferral, then thank him and stop -- there is no useful
    second ask, and run 314's second ask produced a third, firmer no."""
    s = CallState()
    s.offered = ("A", "B")
    triage_apply(s, said)
    assert s.must_end
    assert "Thank them warmly" in s.end_reason


@pytest.mark.parametrize("said", DEFERRALS)
def test_the_same_words_before_the_close_do_not_end_the_call(said):
    """"I'll tell you later" about a QUESTION is not a postponed appointment.
    Hanging up there would lose the lead a different way."""
    s = CallState()
    assert s.offered == ()
    triage_apply(s, said)
    assert not s.must_end


def test_an_accepted_slot_outranks_a_deferral():
    """"రేపు ఉదయం ఓకే, తర్వాత చెప్తాను" is a booking with a comment attached."""
    r = triage("రేపు ఉదయం ఓకే, తర్వాత చెప్తాను")
    assert r.next_step_agreed
    assert not r.deferred


@pytest.mark.parametrize("said", [
    "ఆ ఉంది", "అవును సరే", "మంచిర్యాల్లో", "500 వస్తుంది",
    "మీరు ఏ సోలార్ కంపెనీ?",
])
def test_ordinary_answers_are_not_deferrals(said):
    """A deferral that fires on a plain answer would hang up mid-qualification."""
    assert not triage(said).deferred


def test_a_real_booking_still_books():
    r = triage("రేపు ఉదయం పది గంటలకు కుదురుతుంది")
    assert r.next_step_agreed and not r.deferred


# --- a clock time must not be half English and half Telugu -------------------
#
# Corrected twice, and the rule is neither language. The client, verbatim:
# "రేపు ఉదయం పది గంటలకు this is also okay and రేపు ఉదయం ten o'clock both are
# okay but that ten గంటలు not good."
#
# On 29 Aug everything became గంటలకు, which broke "ten o'clock". On the morning
# of the 30th everything became o'clock, which broke "పది గంటలకు". The defect
# was never the language -- it was mixing them inside one time.

@pytest.mark.parametrize("said", [
    "రేపు ఉదయం పది గంటలకు",      # fully Telugu
    "రేపు ఉదయం ten o'clock",     # fully English
    "ఐదు గంటకి",
])
def test_a_consistent_clock_time_is_left_alone(said):
    assert spoken(said) == said


@pytest.mark.parametrize("said,want", [
    ("రేపు ఉదయం ten గంటలకు", "రేపు ఉదయం ten o'clock"),
    ("టెన్ గంటలకు", "ten o'clock"),
    ("ఫైవ్ గంటకి", "five o'clock"),
])
def test_the_hybrid_is_made_consistent(said, want):
    """An English figure with గంటలకు after it. Transliterated figures count as
    English -- "టెన్ o'clock" would be a third hybrid, not a fix."""
    assert spoken(said) == want


def test_a_duration_is_untouched():
    """"eight to ten గంటలు" is how many hours. It is the form the client asked
    for, and it carries no case marker to catch on."""
    assert spoken("రోజుకి eight to ten గంటలు ఎండ ఉంటుంది") == (
        "రోజుకి eight to ten గంటలు ఎండ ఉంటుంది")


def test_the_bare_singular_is_still_pluralised():
    """The client's other correction: "ఐదు గంట" -> "ఐదు గంటలు"."""
    assert spoken("ఐదు గంట") == "ఐదు గంటలు"


def test_what_the_agent_offers_is_fully_english():
    from datetime import datetime, timedelta

    from api.services.vaani.booking import IST, parse_slot
    when = (datetime.now(IST) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    said = Slot(when=when).say()
    assert "ten o'clock" in said and "గంటలకు" not in said
    assert parse_slot(said) is not None, "the agent must be able to read it back"
