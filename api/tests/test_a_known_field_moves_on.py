"""Run 1027 (23 Sep): it asked for the city it had already written down.

    BOT  మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?     asks the city
    USER విజయవాడ.                                  stored: location = విజయవాడ
    BOT  మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?
    USER లేదు.
    BOT  సరే, మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?
    USER 50,000.
    BOT  మంచిది, మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?   the city AGAIN

The answered-field guard saw it -- `location` was in `known` -- and blocked the
question. What it did next was the problem, and it has now been wrong twice in
one day:

  * Until this morning it substituted REPAIR_LINE, telling a caller it had heard
    perfectly that it had not heard him. Four times in run 1023, and he hung up.
  * Making that line a one-shot fixed the apology loop and let the SECOND block
    fall through to the model's words -- which are the re-ask itself. Run 1027
    asked the city twice and the bill twice.

Both are the same mistake: treating "we must not ask this" as a question about
what to SAY, when it is a question about what to ASK NEXT. The state already
knows -- `still_need` is the list of fields not yet collected, in order, and
`questions` holds the wording for each. So a blocked re-ask becomes the next
question we actually need, the call moves forward, and the caller never hears
either the apology or the repeat.

The repair line survives for the case it was written for: a genuine repeat when
there is nothing left to move on to.
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
    f._repair_suppressed = False
    return f, state


def test_run_1027_the_city_is_not_asked_twice():
    """The exact failure: location known, location asked again."""
    f, _ = _filter({"location": "విజయవాడ"})
    out = f._gate(QUESTIONS["location"])
    assert out != QUESTIONS["location"], "it asked for the city it already had"


def test_it_asks_the_next_thing_it_actually_needs():
    f, _ = _filter({"location": "విజయవాడ"})
    out = f._gate(QUESTIONS["location"])
    assert out == QUESTIONS["property_type"], (
        "property_type is the first field still missing, so that is the "
        f"question the caller should hear; got {out!r}")


def test_it_skips_fields_already_collected_when_choosing():
    f, _ = _filter({"location": "విజయవాడ", "property_type": "own house",
                    "monthly_bill": 50000})
    out = f._gate(QUESTIONS["location"])
    assert out == QUESTIONS["roof_available"], (
        f"roof is the next gap, not something already known; got {out!r}")


def test_it_never_offers_the_apology_when_there_is_somewhere_to_go():
    f, _ = _filter({"location": "విజయవాడ"})
    assert f._gate(QUESTIONS["location"]) != guardrails.REPAIR_LINE


def test_a_field_we_still_need_is_asked_normally():
    """The guard must stay out of the way of ordinary progress."""
    f, _ = _filter({"location": "విజయవాడ"})
    line = QUESTIONS["monthly_bill"]
    assert f._gate(line) == line


def test_with_nothing_left_to_ask_it_does_not_repeat_the_question():
    """Everything collected and the model asks again anyway. There is no next
    question, so falling back is correct -- but it must not be the re-ask."""
    f, _ = _filter({k: "x" for k in QUESTIONS})
    out = f._gate(QUESTIONS["location"])
    assert out != QUESTIONS["location"]
