"""A caller must never be asked the same thing a third time.

Run 218 (2026-08-28, 180s, ended by the caller) is the whole reason this file
exists. He gave his bill on turn 2 -- "టెన్ టు ట్వంటీ లాక్స్" -- and was asked
for it again on turns 3, 15 and 17. Property type was asked three times, his
name three times. He said "చెప్పాను కదా అప్పుడే" (I already told you), then
"ఎన్ని సార్లు అడుగుతారు?" (how many times are you going to ask?), then
"మీరు చాలా ఇన్‌కన్సిస్టెంట్ గా" -- and hung up.

The repeat guard did not catch it because it compares WORDING, and "బిల్లు
ఎంత?" and "బిల్లు సుమారు ఎంత?" are different sentences asking one question.
Extraction is asynchronous by design, so STILL_NEED can lag a turn behind what
the caller has actually said. Neither of those is fixed by better phrasing.

Two independent guards, so a failure in one does not restore the behaviour:

  1. a hard per-field budget of two asks, regardless of extraction
  2. the caller saying they already answered, believed immediately

An unknown field costs a follow-up call. This cost the call itself.
"""

from __future__ import annotations

from api.services.vaani.state import CallState
from api.services.vaani import triage


def state() -> CallState:
    return CallState(
        required_fields=["monthly_bill", "property_type", "name"],
        questions={"monthly_bill": "బిల్లు ఎంత?",
                   "property_type": "ఇల్లా లేకా ఆఫీసా?",
                   "name": "మీ పేరు?"},
    )


def test_a_field_is_never_asked_a_third_time():
    st = state()
    st.render()
    st.render()
    assert "monthly_bill" not in st.still_need
    assert "monthly_bill" in st.abandoned
    # ...and the call moves ON rather than stalling.
    assert st.still_need[0] == "property_type"


def test_the_first_two_asks_are_allowed():
    """One ask plus one clarification is legitimate; the third is not."""
    st = state()
    st.render()
    assert "monthly_bill" in st.still_need, "one ask must not abandon a field"


def test_an_answered_field_leaves_the_checklist_immediately():
    st = state()
    st.render()
    st.learn("monthly_bill", "10-20 లక్షలు")
    assert "monthly_bill" not in st.still_need
    assert "monthly_bill" not in st.abandoned, "answered is not abandoned"


def test_already_told_you_is_believed_at_once():
    """The exact run-218 utterance, and the exact response it should get."""
    st = state()
    st.render()
    triage.apply(st, "అదే 10 టు 20 లాక్స్ చెప్పాను కదా")
    assert "monthly_bill" not in st.still_need


def test_how_many_times_will_you_ask_stops_the_asking():
    st = state()
    st.render()
    triage.apply(st, "ఎన్ని సార్లు అడుగుతారు?")
    assert "monthly_bill" not in st.still_need


def test_an_ordinary_answer_does_not_abandon_anything():
    """The guard must not fire on cooperative callers, or it drops good data."""
    st = state()
    st.render()
    for reply in ["రెండు వేలు", "నా పేరు రవి", "ఇల్లు", "సరే చెప్పండి"]:
        triage.apply(st, reply)
    assert "monthly_bill" in st.still_need, f"still_need={st.still_need}"


def test_the_checklist_empties_rather_than_looping_forever():
    """Run 218's real shape: a caller who answers nothing must still reach an end."""
    st = state()
    for _ in range(20):
        st.render()
    assert st.still_need == []
    assert len(st.abandoned) == 3
