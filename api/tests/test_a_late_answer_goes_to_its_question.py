"""A late yes/no answers the yes/no question, not the name that came after it.

Run 1044 (24 Sep):

    BOT  మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?    roof (a yes/no question)
    USER కొంత. రూపాయలు. ఎందుకు అడుగుతున్నారు?
    BOT  ...మీకు రూఫ్ లేదా టెర్రస్ ఉందా?
    USER సరే, సరే.
    BOT  సరే, మీ పేరు చెప్పగలరా?            name asked
    USER ఆ, ఉంది.                            "yes, there is" -- about the ROOF
    BOT  సైట్ సర్వే ఉచితం...                 moved on; the name is never asked
    USER నా పేరు అడిగారు, మళ్లీ స్కిప్ చేశారు. you asked my name, then skipped it
    BOT  క్షమించండి, సరిగ్గా వినిపించలేదు.    and apologised for not hearing him

Traced in the code: `note_answer_to_last_ask` credits whatever the caller says
to `last_asked`. "ఆ" is a hesitation, "ఉంది" is not, so the whole reply counted
as an answer to the NAME: `customer_name` went to `answered_pending`, its
`answer_counts` to 1, and the checklist moved on. Run 1023 is the same thing the
other way round -- "మాది హైదరాబాద్." arriving after the roof question skipped
the roof.

The fix reads the one fact that settles it: the field's TYPE. A bare yes/no
cannot answer a string field like a name. If the question asked just before was
a yes/no field still open, the reply belongs to that one; otherwise it answers
nothing and the question stays open.

And the caller's complaint itself -- "స్కిప్ చేశారు" -- was matched by nothing:
`NOT_YET_ANSWERED` knew only the English "you skipped".
"""

from api.services.vaani import triage
from api.services.vaani.state import CallState

FIELDS = {
    "property_type": "మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?",
    "roof_available": "మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?",
    "customer_name": "మీ పేరు చెప్పగలరా?",
    "assessment_agreed": "ఉచితంగా ఒక సైట్ సర్వే చేయించుకుంటారా?",
}
TYPES = {"property_type": "string", "roof_available": "boolean",
         "customer_name": "string", "assessment_agreed": "boolean"}


def _after(*asked: str, typed: bool = True) -> CallState:
    """A call where these questions were asked, in this order."""
    s = CallState(required_fields=list(FIELDS), questions=dict(FIELDS))
    if typed:
        s.field_types = dict(TYPES)
    for q in asked:
        s.commit_ask(FIELDS[q])
    return s


def test_run_1044_a_late_yes_does_not_answer_the_name():
    s = _after("roof_available", "customer_name")
    s.note_answer_to_last_ask("ఆ, ఉంది.")
    assert "customer_name" not in s.answered_pending, "yes is not a name"
    assert s.answer_counts.get("customer_name", 0) == 0
    assert "customer_name" in s.still_need


def test_run_1044_the_late_yes_goes_to_the_roof():
    s = _after("roof_available", "customer_name")
    s.note_answer_to_last_ask("ఆ, ఉంది.")
    assert "roof_available" in s.answered_pending, (
        "the yes/no question asked just before is what 'ఉంది' answers")


def test_a_yes_to_a_yes_no_question_still_answers_it():
    """The ordinary case must be untouched."""
    s = _after("customer_name", "roof_available")
    s.note_answer_to_last_ask("ఆ, ఉంది.")
    assert "roof_available" in s.answered_pending


def test_a_real_name_still_answers_the_name():
    s = _after("roof_available", "customer_name")
    s.note_answer_to_last_ask("నా పేరు కైలాష్.")
    assert "customer_name" in s.answered_pending


def test_a_bare_yes_with_no_open_yes_no_question_answers_nothing():
    """The roof is already known, so "అవును" to the name has nowhere to go --
    the name stays open rather than being swallowed."""
    s = _after("roof_available", "customer_name")
    s.known["roof_available"] = True
    s.note_answer_to_last_ask("అవును.")
    assert "customer_name" in s.still_need
    assert "customer_name" not in s.answered_pending


def test_an_agent_with_no_field_types_behaves_exactly_as_before():
    """Other agents may not carry types. Without them, nothing changes."""
    s = _after("roof_available", "customer_name", typed=False)
    s.note_answer_to_last_ask("ఆ, ఉంది.")
    assert "customer_name" in s.answered_pending


def test_the_telugu_you_skipped_it_is_heard():
    s = _after("customer_name")
    s.ask_counts["customer_name"] = 1
    triage.apply(s, "నా పేరు అడిగారు, మళ్లీ స్కిప్ చేశారు.")
    assert s.ask_counts.get("customer_name", 0) == 0, (
        "he is telling us the question was dropped; give it back")
