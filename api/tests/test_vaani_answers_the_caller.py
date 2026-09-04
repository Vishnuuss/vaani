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
    """The CHECKLIST stays withdrawn. That is what run 218 was about.

    The bare command "ASK THIS, IN THESE EXACT WORDS", sitting underneath a
    LIST OF FIELDS, is what made the agent ask his name instead of answering.
    The list is still gone. What comes back below is one ordered instruction
    with the answer first -- a different thing, and the client asked for it by
    name on 4 Sep: "it should not skip questions".
    """
    block = state(text).render()
    assert "STILL_NEED: []" in block
    assert "ASK THIS, IN THESE EXACT WORDS" not in block


@pytest.mark.parametrize("text", ASKED)
def test_the_answer_comes_first_and_the_question_second(text):
    """Answer, THEN ask -- in that order, in one reply.

    Withdrawing the question entirely made the call stall: measured on run 575,
    the caller asked four things, was answered correctly four times, and the
    agent asked its own question on only one of those turns. Qualification
    stopped dead every time he was curious.
    """
    block = state(text).render()
    assert "FIRST answer" in block
    assert "THEN, in the SAME reply" in block
    assert block.index("FIRST answer") < block.index("THEN, in the SAME reply")


@pytest.mark.parametrize("text", ASKED)
def test_a_long_answer_may_stand_alone(text):
    """The escape hatch. Answering AND interrogating in one breath is run 218."""
    block = state(text).render()
    assert "leave the question out entirely" in block


@pytest.mark.parametrize("text", ASKED)
def test_the_field_is_still_charged_when_the_question_is_appended(text):
    """If the question is asked, its ask budget must be spent -- otherwise the
    two-ask cap cannot see it and the agent can ask forever."""
    st = state(text)
    block = st.render()
    if "THEN, in the SAME reply" in block and "leave the question out" in block:
        assert st.pending_ask, "asked a field without charging it"


def test_the_checklist_returns_on_the_very_next_turn():
    """Suppression is for one turn. The fields are still needed."""
    st = state(ASKED[0])
    st.render()
    st.last_user_text = "మాది కంపెనీ."
    block = st.render()
    assert "ASK THIS, IN THESE EXACT WORDS" in block
    assert "monthly_bill" in block


def test_an_ordinary_answer_still_gets_the_next_question():
    block = state("మాది కంపెనీ.").render()
    assert "ASK THIS, IN THESE EXACT WORDS" in block
