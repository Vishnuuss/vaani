"""When the caller asks something, that gets answered -- nothing else.

Run 218, the call the client complained about. The caller asked three real
questions and was interrogated instead of answered:

  turn 10  "మేము యాక్చువల్గా సిలికాన్ వ్యాలీ తెలుస్తదా?"
           -> agent asked about his roof
  turn 13  "కాలిఫోర్నియాలో ఉంటాము, అక్కడ సోదాలు పెట్టాలనుకుంటున్నారా?"
           -> agent asked his property type
  turn 18  "కాదు పాసిబుల్ అయి ఉంది యా ... ఇట్స్ నాట్ ఏ స్మాల్ ప్లాట్"
           -> agent asked his name

By turn 36 he said it plainly: "అతను సోలార్ పెట్టొచ్చా అని అడిగాను, మీరేమో పేరు
అడుగుతున్నారు" -- I asked whether solar can be installed, you are asking my name.

Detection was NOT the failure. It fired correctly on turns 10 and 13 and the
agent asked anyway, because the state block showed STILL_NEED and NEXT QUESTION
TO ASK alongside a prose line saying to answer first. `state.py` already
documents that the checklist beats prose. So the checklist is withdrawn for that
turn, which is the one thing known to work.
"""

from __future__ import annotations

import pytest

from api.services.vaani.state import CallState, _is_question

# Verbatim from run 218.
ASKED = [
    "మేము యాక్చువల్గా సిలికాన్ వ్యాలీ తెలుస్తదా?",
    "కాలిఫోర్నియాలో ఉంటాము, అక్కడ సోదాలు పెట్టాలనుకుంటున్నారా?",
    "కాదు పాసిబుల్ అయి ఉంది యా బికాజ్ ఇట్స్ నాట్ ఏ స్మాల్ ప్లాట్ ఇట్స్ లైక్ బిగ్ వన్.",
    "ఎన్ని సార్లు అడుగుతారు?",
]

# Also verbatim. These are answers and fillers; treating any of them as a
# question would suppress the checklist and stall the call.
NOT_ASKED = [
    "ఆ. పెద్ద కంపెనీ ఉంది, ఆ కంపెనీ మీద పెట్టాలి మేము యాక్చువల్లీ.",
    "ఆ. వచ్చేసి ఒక అప్రాక్సిమేట్ గా టెన్ టు ట్వంటీ లాక్స్ ఉండదండి",
    "షిక్ హితేష్.",
    "మాది కంపెనీ.",
    "ఉంది ఉంది.",
    "చెప్పండి.",
    "అది చాలా పెద్దది.",
]


def state(last_user_text: str) -> CallState:
    st = CallState(
        required_fields=["monthly_bill", "customer_name"],
        questions={"monthly_bill": "బిల్లు ఎంత?", "customer_name": "మీ పేరు?"},
    )
    st.last_user_text = last_user_text
    return st


@pytest.mark.parametrize("text", ASKED)
def test_a_caller_question_is_recognised(text):
    assert _is_question(text), f"missed a real question: {text}"


@pytest.mark.parametrize("text", NOT_ASKED)
def test_an_answer_is_not_mistaken_for_a_question(text):
    """A false positive here stalls the call, so it is the costlier direction."""
    assert not _is_question(text), f"a plain answer read as a question: {text}"


@pytest.mark.parametrize("text", ASKED)
def test_the_checklist_is_withdrawn_while_answering(text):
    block = state(text).render()
    assert "STILL_NEED: []" in block
    assert "NEXT QUESTION TO ASK" not in block, (
        "showing the next question is exactly what made the agent ask his name "
        "instead of answering whether solar was possible")


@pytest.mark.parametrize("text", ASKED)
def test_no_ask_is_charged_for_a_turn_spent_answering(text):
    st = state(text)
    st.render()
    assert st.pending_ask == "", (
        "a turn spent answering must not consume a field's ask budget")


def test_the_checklist_returns_on_the_very_next_turn():
    """Suppression is for one turn. The fields are still needed."""
    st = state(ASKED[0])
    st.render()
    st.last_user_text = "మాది కంపెనీ."
    block = st.render()
    assert "NEXT QUESTION TO ASK" in block
    assert "monthly_bill" in block


def test_an_ordinary_answer_still_gets_the_next_question():
    block = state("మాది కంపెనీ.").render()
    assert "NEXT QUESTION TO ASK" in block
