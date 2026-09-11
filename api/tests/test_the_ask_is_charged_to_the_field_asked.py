"""Runs 872, 879 and 885 (11 Sep): the two-ask cap never bound, so he was asked again.

    BOT   మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?        how much is your bill?
    BOT   సరే, మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?     (roof, stacked on the same turn)
    USER  ఉమ్ ఉమ్ ఉమ్. హలో.
    BOT   నెలకి కరెంట్ బిల్లు ఎంత అవుతుంది అండి?      the bill AGAIN, reworded
    ...
    BOT   సరే, మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?     the roof AGAIN
    USER  చెప్తున్నా కదా హైదరాబాద్ అని                I am TELLING you, Hyderabad

`MAX_ASKS_PER_FIELD = 2` existed and was correct. It never bound because
`commit_ask()` charged `pending_ask` -- the field the state block NOMINATED --
rather than the field the sentence actually asked about. The codebase already
recorded that the two diverge (state.py: "the state correctly nominated
monthly_bill and the model asked about location instead"). When they diverge the
budget is spent on the wrong field and the repeated field is never bounded.

`_is_repeat` could not catch it either: it compares WORDS, and run 879's two
location asks were worded differently. A guard on wording cannot catch a repeat
of subject.
"""

from api.services.vaani.state import CallState


def _solar() -> CallState:
    """MB Solar's real questions, as configured on workflow 2."""
    s = CallState()
    s.questions = {
        "property_type": "మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?",
        "monthly_bill": "మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?",
        "location": "మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?",
        "roof_available": "మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?",
        "customer_name": "మీ పేరు చెప్పగలరా?",
    }
    s.required_fields = list(s.questions)
    return s


# --- it must recognise the subject, not the wording ------------------------

def test_it_reads_the_subject_off_the_sentence_the_agent_actually_said():
    s = _solar()
    assert s.field_asked_in("మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?") == "monthly_bill"
    assert s.field_asked_in("సరే, మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?") == "roof_available"


def test_run_885_the_reworded_bill_ask_is_still_the_bill():
    """"నెలకి కరెంట్ బిల్లు ఎంత అవుతుంది అండి?" -- different words, same subject."""
    assert _solar().field_asked_in("నెలకి కరెంట్ బిల్లు ఎంత అవుతుంది అండి?") == "monthly_bill"


def test_run_872_the_reworded_property_ask_is_still_the_property():
    s = _solar()
    assert s.field_asked_in("మీరు సొంత ఇల్లు, అపార్ట్‌మెంట్ లేదా కమర్షియల్ స్థలం ఏది?") == "property_type"


def test_a_sentence_that_asks_nothing_on_the_checklist_charges_nothing():
    s = _solar()
    assert s.field_asked_in("థాంక్యూ అండి.") == ""
    assert s.field_asked_in("మా ఆఫీస్ విజయవాడ, MG Road లో ఉంది అండి.") == ""


# --- the budget is charged where it was spent ------------------------------

def test_the_ask_is_charged_to_the_field_asked_not_the_field_nominated():
    """Run 885's exact divergence: state nominated the bill, the model asked the roof."""
    s = _solar()
    s.pending_ask = "monthly_bill"
    s.commit_ask("సరే, మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?")
    assert s.ask_counts.get("roof_available") == 1
    assert s.ask_counts.get("monthly_bill", 0) == 0


def test_run_885_the_roof_is_not_asked_a_third_time():
    s = _solar()
    for said in ("సరే, మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?",
                 "మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?"):
        s.pending_ask = "monthly_bill"      # nominated something else each time
        s.commit_ask(said)
    assert s.ask_counts["roof_available"] == s.MAX_ASKS_PER_FIELD
    assert "roof_available" not in s.still_need


def test_it_falls_back_to_the_nominated_field_when_nothing_matches():
    """A client with no written questions must behave exactly as before."""
    s = CallState()
    s.pending_ask = "monthly_bill"
    s.commit_ask("whatever the model said")
    assert s.ask_counts.get("monthly_bill") == 1


def test_commit_ask_with_no_text_still_works():
    """The start-frame call site passes nothing; it must not start counting wrong."""
    s = _solar()
    s.pending_ask = "location"
    s.commit_ask()
    assert s.ask_counts.get("location") == 1
