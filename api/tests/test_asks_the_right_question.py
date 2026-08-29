"""Run 298: the caller could not work out which bill was being asked about.

    BOT   సరే, మీ నెలవారీ బిల్లు ఎంత?          how much is your monthly bill?
    USER  ఏం బిల్లు అండి                       what bill?
    BOT   మంచిది, మీ నెలవారీ బిల్లు ఎంత?       (asks the identical thing again)
    USER  అది ఏం బిల్లు?                       WHAT bill?
    BOT   అది మీ ఇంటి నెలవారీ విద్యుత్ బిల్లు   that is your HOUSE's monthly bill

Two separate failures in five lines, and neither is about solar:

1. The words being spoken were never written to be spoken. They came from
   `extraction_variables[].prompt`, which the DTO documents as the "Extraction
   Hint" -- "Monthly electricity bill in rupees". The model translated that on
   the fly and dropped the one word the caller needed.
2. Nobody had mentioned a house. `property_type` was null and unasked; the man
   could have been ringing from a factory. The model filled the gap because a
   fluent sentence wants a noun there.
"""

from api.services.vaani.compiler import spoken_question as _spoken_question
from api.services.vaani.state import CallState


# --- what the caller hears is not the schema description -------------------

def test_the_spoken_question_wins_over_the_extraction_hint():
    variable = {"name": "monthly_bill",
                "prompt": "Monthly electricity bill in rupees",
                "ask": "మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?"}
    assert _spoken_question(variable) == "మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?"


def test_a_variable_with_no_spoken_question_behaves_exactly_as_before():
    """Every workflow that predates this change must be unaffected."""
    variable = {"name": "location", "prompt": "City or area they live in"}
    assert _spoken_question(variable) == "City or area they live in"


def test_an_empty_spoken_question_falls_back_rather_than_going_silent():
    variable = {"name": "location", "prompt": "City or area", "ask": ""}
    assert _spoken_question(variable) == "City or area"


def test_it_reads_objects_as_well_as_dicts():
    class Variable:
        name, prompt, ask = "monthly_bill", "hint", "మీ కరెంట్ బిల్లు ఎంత?"
    assert _spoken_question(Variable()) == "మీ కరెంట్ బిల్లు ఎంత?"


def test_an_object_without_the_new_attribute_still_works():
    """DTOs built before `ask` existed have no such attribute at all."""
    class OldVariable:
        name, prompt = "location", "City or area"
    assert _spoken_question(OldVariable()) == "City or area"


# --- it may not invent the caller's situation ------------------------------

def _state() -> CallState:
    s = CallState()
    s.required_fields = ["monthly_bill", "property_type", "location"]
    s.questions = {f: f for f in s.required_fields}
    return s


def test_the_agent_is_told_what_it_has_not_been_told():
    block = _state().render()
    assert "NOT TOLD YET" in block
    assert "property_type" in block


def test_a_field_already_answered_is_not_listed_as_unknown():
    s = _state()
    s.learn("property_type", "commercial")
    block = s.render()
    assert "property_type" not in block.split("NOT TOLD YET")[1].split("\n")[0]


def test_the_warning_disappears_once_everything_is_known():
    s = _state()
    for f in s.required_fields:
        s.learn(f, "x")
    assert "NOT TOLD YET" not in s.render()
