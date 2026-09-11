"""Run 887 (11 Sep), the caller's last words before the call timed out:

    USER  సైట్ సర్వే సైట్ సర్వే కొంచెం పక్కన పెడతారా దాన్ని,
          డౌట్స్ క్లియర్ చేయండి ఫస్ట్
          "put the site survey aside -- clear my doubts FIRST"

He had already said it three other ways. He asked who the vendor was, which
company this is, whether they had installed for anyone before -- and each answer
was followed by another qualification question. He was not refusing and he was
not asking to be listened to in silence. He was asking us to STOP ASKING and
START ANSWERING, which is a third thing, and nothing in triage recognised it.

`WANTS_THE_FLOOR` is the closest existing intent and it is the wrong shape: its
branch says "invite him to go on and then STOP TALKING". This caller does not
want the floor. He wants an answer.

Run 885 is the same intent, angrier, one call earlier:

    USER  ఆ నా క్వశ్చన్ కి ఆన్సర్ ఇవ్వండి        ANSWER MY QUESTION
"""

import pytest

from api.services.vaani import triage
from api.services.vaani.state import CallState

HEARD = [
    "సైట్ సర్వే కొంచెం పక్కన పెడతారా దాన్ని డౌట్స్ క్లియర్ చేయండి ఫస్ట్",
    "ఆ నా క్వశ్చన్ కి ఆన్సర్ ఇవ్వండి",
    "నా డౌట్స్ క్లియర్ చేయండి",
    "ముందు నా ప్రశ్నకి సమాధానం చెప్పండి",
    "answer my question first",
    "clear my doubts first",
]

NOT_HEARD = [
    "హైదరాబాద్లో ఉంటాను.",
    "70 లాక్స్",
    "ఆ ఉంది ఉంది.",
    "సరే చేయించుకుంటాను",
]


@pytest.mark.parametrize("text", HEARD)
def test_he_is_asking_to_be_answered_not_qualified(text):
    assert triage.triage(text).answer_me_first, text


@pytest.mark.parametrize("text", NOT_HEARD)
def test_an_ordinary_answer_is_not_mistaken_for_it(text):
    assert not triage.triage(text).answer_me_first, text


def _state() -> CallState:
    return CallState(required_fields=["monthly_bill", "location"],
                     questions={"monthly_bill": "మీ కరెంట్ బిల్లు నెలకి ఎంత?",
                                "location": "మీరు ఏ ఊరు?"})


def test_the_checklist_is_withdrawn_while_he_wants_answers():
    s = _state()
    triage.apply(s, "డౌట్స్ క్లియర్ చేయండి ఫస్ట్")
    assert s.answer_me_first
    block = s.render()
    assert "ANSWER HIS DOUBTS" in block
    assert "Ask NOTHING" in block
    # Removing the list is what works. Prose telling the model to wait does not
    # beat a list of fields at the end of the context -- this file's own lesson.
    assert "STILL_NEED: []" in block
    assert s.pending_ask == ""


def test_qualification_resumes_once_he_has_no_more_doubts():
    """Latched for the turn it was asked on, and not beyond it.

    Left standing, the checklist would be withdrawn for the rest of the call
    and nothing would ever be qualified again. If he has another doubt, triage
    re-latches it from what he says.
    """
    s = _state()
    triage.apply(s, "డౌట్స్ క్లియర్ చేయండి ఫస్ట్")
    s.render()
    s.end_user_turn()
    triage.apply(s, "సరే అర్థమైంది")
    s.last_user_text = "సరే అర్థమైంది"
    block = s.render()
    assert not s.answer_me_first
    assert "ANSWER HIS DOUBTS" not in block
