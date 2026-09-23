"""Run 891 (12 Sep): it asked for things it had already written down.

    t3  USER కమర్స్ సెలూన్                    property answered
    t4  BOT  సరే, 50 లాక్స్. మీరు ఏ ఏరియా...   bill confirmed back to him
    t5  BOT  మీది సొంత ఇల్లా, అపార్ట్‌మెంటా...?  property AGAIN
    t6  BOT  మీ కరెంట్ బిల్లు నెలకి ఎంత...?     the bill AGAIN
    t7  BOT  సరే, వినిపించలేదు. మీది సొంత...?   property a THIRD time

and run 890, three turns after he had answered:

    USER చెప్పాను కదా కమర్షియల్ అని ఫస్ట్ లోనే చెప్పాను కదా
         I told you commercial -- I said it right at the start

Neither guard could catch this.

`MAX_ASKS_PER_FIELD` counts asks, and two asks is a legal budget -- it exists
for a field we have NOT got. It has nothing to say about a field already in
`known`, which should never be asked even once more.

`_is_repeat` compares the wording of the new sentence against what was said
before, and the re-asks are not always worded alike. That is the same lesson as
the ask budget: a guard on wording cannot catch a repeat of subject.

So the subject is read off the sentence with `field_asked_in` -- the matcher
already written for the ask budget -- and if the answer is a field we already
hold, the question is never spoken. Substituted, not truncated, and only while
nothing has been spoken yet: half a sentence is worse than the repeat, which is
run 783's "అర్థమైంది బిల్లు?".
"""

from api.services.vaani import guardrails
from api.services.vaani.reply_sanitizer import ReplySanitizer
from api.services.vaani.state import CallState

QUESTIONS = {
    "property_type": "మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?",
    "monthly_bill": "మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?",
    "location": "మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?",
    "roof_available": "మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?",
    "customer_name": "మీ పేరు చెప్పగలరా?",
}


def _filter(known: dict):
    from api.services.vaani.brain_processor import ReplyFilter

    state = CallState(required_fields=list(QUESTIONS), questions=dict(QUESTIONS))
    state.known.update(known)

    class _Injector:
        pass

    inj = _Injector()
    inj.state = state

    f = ReplyFilter.__new__(ReplyFilter)
    f._injector = inj
    f._sanitizer = ReplySanitizer()
    f._spoken = ""
    f._blocked = False
    f._said = []
    f._stale = False
    f._pending_repeat = ""
    return f


# What a blocked re-ask becomes changed on 23 Sep. These tests still pin the
# thing that matters -- the known field is never asked again -- but the
# substitution is now the next question we actually need, not REPAIR_LINE.
# Run 1023 showed the apology repeating until the caller hung up, and run 1027
# showed the re-ask getting through once the apology was made a one-shot. Both
# were answers to "what should it SAY"; the right question was "what should it
# ask NEXT". See test_a_known_field_moves_on.py.


def test_run_891_the_property_is_not_asked_once_it_is_known():
    f = _filter({"property_type": "commercial"})
    out = f._gate(QUESTIONS["property_type"])
    assert out != QUESTIONS["property_type"]
    assert out == QUESTIONS["monthly_bill"], "the next field we still need"


def test_run_891_the_bill_is_not_asked_once_it_is_written_down():
    f = _filter({"monthly_bill": "5000000"})
    out = f._gate(QUESTIONS["monthly_bill"])
    assert out != QUESTIONS["monthly_bill"]
    assert out == QUESTIONS["property_type"]


def test_a_reworded_re_ask_of_a_known_field_is_caught_too():
    """Wording differs; the subject does not. That is the whole point."""
    f = _filter({"property_type": "commercial"})
    out = f._gate("మీరు సొంత ఇల్లు, అపార్ట్‌మెంట్ లేదా కమర్షియల్ స్థలం ఏది?")
    assert out == QUESTIONS["monthly_bill"]


def test_a_field_we_do_not_have_is_asked_normally():
    f = _filter({"property_type": "commercial"})
    line = QUESTIONS["location"]
    assert f._gate(line) == line


def test_an_ordinary_answer_is_not_blocked():
    """Only QUESTIONS about known fields. Mentioning one is fine."""
    f = _filter({"location": "Hyderabad"})
    line = "మా ఆఫీస్ విజయవాడ, MG Road లో ఉంది అండి."
    assert f._gate(line) == line


def test_nothing_is_cut_off_once_it_has_started_speaking():
    """Half a sentence is worse than the repeat -- run 783."""
    f = _filter({"property_type": "commercial"})
    f._spoken = "సరే అండి, "
    line = QUESTIONS["property_type"]
    assert f._gate(line) == line
