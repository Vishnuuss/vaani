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


# --- "ten oclock" ------------------------------------------------------------

def test_the_offer_line_says_gantalaku():
    """The English NUMBER is deliberate and stays. "oclock" is not a number --
    it is a bare English word dropped into a Telugu sentence."""
    from datetime import datetime, timedelta

    from api.services.vaani.booking import IST
    when = (datetime.now(IST) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    said = Slot(when=when).say()
    assert "గంటలకు" in said
    assert "oclock" not in said
    assert "ten" in said, "the hour stays in English, as the client asked"


def test_the_sanitizer_catches_oclock_the_model_wrote_itself():
    """booking.py is not the only source -- the model copies the pattern into
    its own sentences after seeing it all call."""
    out = spoken("రేపు ఉదయం ten oclock లేదా ఎల్లుండి సాయంత్రం four oclock")
    assert "oclock" not in out
    assert out == "రేపు ఉదయం ten గంటలకు లేదా ఎల్లుండి సాయంత్రం four గంటలకు"


def test_the_telugu_case_suffix_is_not_left_dangling():
    """Run 300 said "four oclockకి". Naive replacement gives "గంటలకుకి"."""
    out = spoken("ఎల్లుండి సాయంత్రం four oclockకి షెడ్యూల్ చేసాం")
    assert "గంటలకుకి" not in out
    assert "four గంటలకు షెడ్యూల్" in out


def test_already_correct_text_is_untouched():
    assert spoken("రేపు ఉదయం ten గంటలకు") == "రేపు ఉదయం ten గంటలకు"


# --- apologise, then ask the SAME thing -------------------------------------

def test_the_models_own_apology_is_recognised():
    """REPAIR_LINE is only reached when the repetition guard fires. Run 314's
    apology was written by the model itself and matched nothing."""
    from api.services.vaani.guardrails import SAID_NOT_HEARD

    assert SAID_NOT_HEARD.search(
        "సారీ, మీరు ఏ ఏరియా లేదా సిటీలో ఉన్నారో వినిపించలేదు. "
        "మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?")
    assert not SAID_NOT_HEARD.search("మంచిది, మీ పేరు చెప్పగలరా?")


def test_the_state_block_says_ask_the_same_question_again():
    s = CallState(required_fields=["location"],
                  questions={"location": "మీరు ఏ సిటీలో ఉంటున్నారు?"})
    s.misheard_last_turn = True
    assert "ask the SAME" in s.render()


def test_that_line_is_absent_on_an_ordinary_turn():
    """It is re-read and re-billed on every turn it appears on."""
    s = CallState(required_fields=["location"],
                  questions={"location": "మీరు ఏ సిటీలో ఉంటున్నారు?"})
    assert "ask the SAME" not in s.render()
