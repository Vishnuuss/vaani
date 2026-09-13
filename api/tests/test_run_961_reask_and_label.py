"""The two defects run 961 put on the wire, 13 Sep 2026.

Both were heard by the client on a live call to MB Solar Hub and both are
reproduced here from his own words.

1. The agent SAID A FIELD NAME OUT LOUD:

       BOT: "assessment_agreed: ఉచితంగా ఒక సైట్ సర్వే చేయించుకుంటారా?"
       USER: "నీకు మెంటర్ రన్నింగ్ ఏమన్నా?"   -- is something running in you

   `ASSESSMENT_AGREED` was the loudest token in the last line of the prompt,
   in a sentence instructing the model what to say, so it said it.

2. A FIELD HE HAD ANSWERED CAME BACK. He gave the location once and was asked
   twice more, ending in "నేను అనంతపురం ఎన్ని సార్లు చెప్పాలా?" -- how many
   times do I have to say Ananthapuram. The finished record holds
   location: "అనంతపురం, హైదరాబాద్", so the extractor understood him perfectly.
   It was given exactly one turn to prove it and needed more.

The third thing run 961 showed is NOT fixed here and must not be claimed as
fixed: on turn five the state block nominated `monthly_bill` and the model
asked `property_type` instead. The instruction was right and was ignored. That
is model drift, it is a separate defect, and no test in this file covers it.
"""

from __future__ import annotations

import re

import pytest

from api.services.vaani.state import CallState

QUESTIONS = {
    "property_type": "మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?",
    "monthly_bill": "మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?",
    "location": "మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?",
    "roof_available": "మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?",
    "customer_name": "మీ పేరు చెప్పగలరా?",
    "assessment_agreed": "ఉచితంగా ఒక సైట్ సర్వే చేయించుకుంటారా?",
}


def _state() -> CallState:
    s = CallState()
    s.required_fields = list(QUESTIONS)
    s.questions = dict(QUESTIONS)
    return s


def _walk_to(s: CallState, field: str) -> CallState:
    """Fill everything ahead of `field` so it is the live question."""
    for f in QUESTIONS:
        if f == field:
            break
        s.known[f] = "x"
    return s


def _ask_line(state: CallState) -> str:
    m = re.search(r"ASK THEM ABOUT .*", state.render())
    return m.group(0) if m else ""


# ------------------------------------------------------- 1. the spoken label

@pytest.mark.parametrize("field", sorted(QUESTIONS))
def test_every_turn_forbids_saying_a_field_id(field):
    """The prohibition governs the whole block, not one branch of it.

    `assessment_agreed` is why this is asserted on `render()` rather than on
    the ASK line: it is a booking field and renders through the offer branch,
    which emits no ASK line at all. It still appears in STILL_NEED and
    NOT TOLD YET, and it is the exact id the caller heard on run 961, so a
    guard that only covered the ASK line would have missed the real case.
    """
    block = _walk_to(_state(), field).render()
    assert "NEVER say one out loud" in block, (
        f"{field}: nothing in the block forbids reading an id aloud")


@pytest.mark.parametrize("field", sorted(QUESTIONS))
def test_the_id_is_never_the_headline_of_an_ask(field):
    """Where an ASK line exists, its loudest words must not be an identifier."""
    line = _ask_line(_walk_to(_state(), field))
    if not line:
        pytest.skip(f"{field} renders through a branch with no ASK line")
    headline = line.split(" AND NOTHING ELSE")[0]
    assert "_" not in headline, (
        f"{field}: raw id in the loudest part of the instruction: {headline!r}")


def test_assessment_agreed_is_the_run_961_case():
    """Named explicitly, because this is the id the caller actually heard."""
    block = _walk_to(_state(), "assessment_agreed").render()
    assert "assessment_agreed" in block, "fixture drifted; the id should be listed"
    assert "NEVER say one out loud" in block


# ------------------------------------------------- 2. the re-ask: NOT fixed

def test_the_state_machine_nominates_correctly_so_the_loop_is_model_drift():
    """Run 961 turn five, replayed. This documents a defect, it does not fix it.

    The state block said ASK MONTHLY_BILL. The agent asked property_type -- for
    the third time -- and the caller said "కమర్షియల్ అండి, ఒక్కసారి చెప్తే
    వినపడదా?". Every mechanism here was right: `field_asked_in` charged all
    three property_type asks to property_type, the budget was spent, and the
    field had left `still_need`. The instruction was correct and was ignored.

    An attempt to fix this by holding an answered field back for two turns
    instead of one was written and thrown away: it broke
    `test_the_suppression_lasts_exactly_one_turn` and
    `test_two_substantive_answers_retire_the_field_even_with_no_value`, both of
    which encode a deliberate choice. A field must return the next turn so the
    caller gets his second answer, because `answer_counts` is what finally
    retires it. Suppressing it longer is the same trade that took capture from
    6/6 to 1/6 in the guards reverted in 7be3330.

    So this test asserts what is TRUE today, and will fail the day the real fix
    lands -- which is the point.
    """
    s = _state()
    seen = []
    for bot, user in [
        (QUESTIONS["property_type"], "ఇప్పుడే ఏంటి?"),
        ("మీరు ఇల్లు, అపార్ట్‌మెంట్, లేదా కమర్షియల్ స్థలం ఏది?", "కమర్షియల్."),
        (QUESTIONS["monthly_bill"], "60 లాక్స్"),
        (QUESTIONS["location"], "హైదరాబాద్."),
    ]:
        seen.append(s.still_need[0] if s.still_need else None)
        s.commit_ask(bot)
        s.note_answer_to_last_ask(user)
        s.end_user_turn()

    assert s.ask_counts["property_type"] == 2, "both asks were charged correctly"
    assert "property_type" not in s.still_need, (
        "the budget was spent, so the state block did NOT ask for it again")
    assert "property_type" in s.abandoned


def test_the_ask_is_charged_to_the_field_the_sentence_is_about():
    """The matcher, on the real wordings from run 961."""
    s = _state()
    cases = [
        (QUESTIONS["property_type"], "property_type"),
        ("మీరు ఇల్లు, అపార్ట్‌మెంట్, లేదా కమర్షియల్ స్థలం ఏది?", "property_type"),
        ("అవును, మీకు అవసరమైన ఇన్ఫర్మేషన్ ఇస్తాను. " + QUESTIONS["monthly_bill"],
         "monthly_bill"),
        ("సారీ సార్, కొంచెం లేటెన్సీ ఉంది. " + QUESTIONS["roof_available"],
         "roof_available"),
        ("సరే అండి, " + QUESTIONS["location"], "location"),
    ]
    for said, expected in cases:
        assert s.field_asked_in(said) == expected, (
            f"charged to the wrong field: {said[:40]!r}")
