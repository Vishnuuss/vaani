"""Run 853: he answered, and was asked the same thing again in the same breath.

    bot : మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?      which area or city?
    user: ఆనందపూర్ అండి.                            Anandpur.
    bot : మంచిది, ఆనందపూర్. మీరు ఏ నగరం లేదా       good, Anandpur. which city or
          ప్రాంతంలో నివసిస్తున్నారు?                 region do you live in?
    user: కరెంట్ అఫైర్ అని చెప్తున్నారా మళ్ళీ       why are you asking again?
          ఎందుకు చెప్తున్నారు?

Two mechanisms had to both fail for that to reach the caller, and both did.

1. `extractor.py` is ASYNC and off the critical path on purpose -- it is worth
   ~0.3s a turn -- so its verdict lands a turn LATE. Until it does, `known` has
   no `location`, `still_need` still lists it, and the state block is the last
   and most authoritative thing the model reads. It asked again because we told
   it to.

2. The repeat guard did not catch it, and this is the half that generalises:
   `_is_repeat` compares the WORDS of two replies. The two questions were
   worded differently, so it saw two different strings. A guard on wording
   cannot catch a repeat of SUBJECT.

The same call asked his name twice and the site survey three times, so this was
never about locations.
"""

from __future__ import annotations

from api.services.vaani.state import CallState


def _state() -> CallState:
    s = CallState()
    s.required_fields = ["property_type", "monthly_bill", "location",
                         "customer_name", "assessment_agreed"]
    s.questions = {f: f"tell me your {f}" for f in s.required_fields}
    return s


def test_a_field_just_answered_leaves_still_need_for_one_turn():
    s = _state()
    assert "location" in s.still_need
    s.last_asked = "location"
    s.note_answer_to_last_ask("ఆనందపూర్ అండి")
    assert "location" not in s.still_need, (
        "the field he just answered is still being demanded, which is the "
        "sentence run 853's caller objected to")


def test_a_backchannel_is_not_an_answer():
    """"ఆ" is listening, not answering. Treating it as an answer would drop the
    question silently and store a null.

    "సరే" is deliberately NOT in this list, and the distinction is the whole
    point. It is not a hesitation noise, it is the word "okay" -- and to
    "shall we book a free site survey?" it IS the answer. Ruling it out would
    re-ask a man the question he just said yes to, which is the exact defect
    this file exists to stop. A one-word acknowledgement misread as an answer to
    an OPEN field costs one turn, because the suppression lasts one turn and the
    two-ask budget still bounds it; misreading a yes as noise costs the booking.
    """
    s = _state()
    s.last_asked = "location"
    for filler in ("ఆ", "హా", "అం", "hmm", "ఉమ్"):
        s.note_answer_to_last_ask(filler)
        assert "location" in s.still_need, f"{filler!r} was taken as an answer"


def test_an_acknowledgement_answers_a_yes_no_field():
    """The other side of the same coin: "సరే" to "shall we book?" is a yes."""
    s = _state()
    s.last_asked = "assessment_agreed"
    s.note_answer_to_last_ask("సరే")
    assert "assessment_agreed" not in s.still_need


def test_a_question_back_is_not_an_answer():
    """He asked US something. Our question is still open."""
    s = _state()
    s.last_asked = "location"
    s.note_answer_to_last_ask("ఎందుకు అడుగుతున్నారు?")
    assert "location" in s.still_need


def test_the_suppression_lasts_exactly_one_turn():
    """If the extractor finds nothing -- he dodged, or STT garbled it -- the
    field must come back, bounded by the two-ask budget."""
    s = _state()
    s.last_asked = "location"
    s.note_answer_to_last_ask("ఆనందపూర్ అండి")
    assert "location" not in s.still_need
    s.last_asked = "monthly_bill"                 # next turn, different subject
    s.note_answer_to_last_ask("పదిహేను వేలు")
    assert "location" in s.still_need, "the dodged field never came back"


def test_a_confirmed_field_never_comes_back():
    s = _state()
    s.last_asked = "location"
    s.note_answer_to_last_ask("ఆనందపూర్ అండి")
    s.known["location"] = "Anandpur"              # the extractor landed
    s.last_asked = "customer_name"
    s.note_answer_to_last_ask("కృష్ణ")
    assert "location" not in s.still_need


def test_nothing_is_suppressed_when_no_question_was_asked():
    """An unprompted remark must not silently retire a field nobody asked for."""
    s = _state()
    s.last_asked = ""
    s.note_answer_to_last_ask("ఆనందపూర్ అండి")
    assert s.answered_pending == set()
    assert "location" in s.still_need


def test_the_two_ask_budget_still_bounds_a_dodger():
    """Suppression must not become a way to escape MAX_ASKS_PER_FIELD."""
    s = _state()
    s.ask_counts["location"] = CallState.MAX_ASKS_PER_FIELD
    s.last_asked = "location"
    s.note_answer_to_last_ask("ఆనందపూర్ అండి")
    assert "location" not in s.still_need
    assert "location" in s.abandoned
