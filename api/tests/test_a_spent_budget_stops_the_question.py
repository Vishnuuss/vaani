"""Run 898 (12 Sep): the roof was asked four times, and never answered directly.

    36:19  BOT   సరే మంచిది. మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?
    36:32  USER  మాకు పెద్ద ఫ్యాక్టరీ ఉందండి ప్రస్తుతానికి
    36:39  BOT   మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?
    36:42  USER  మాకు ఫ్యాక్టరీ ఉంది
    36:44  BOT   మీకు రూఫ్ లేదా టెర్రస్ ఉందా?
    36:48  USER  ఆ ఫ్యాక్టరీ ఫ్యాక్టరీ మాదిరి.

`MAX_ASKS_PER_FIELD` was charged correctly -- the matcher reads all three of
those sentences as `roof_available` -- so the field left STILL_NEED after two.
Nothing stopped the model asking it anyway.

The existing guard blocks a question about a field already in `known`, and this
field was not known: he answered indirectly, by naming a factory, and never
said yes or no. Out of budget is not the same state as answered, and only one
of the two was being enforced.

Both now block. A field whose budget is spent has had every question it is ever
going to get.
"""

from api.services.vaani import guardrails
from api.services.vaani.brain_processor import ReplyFilter
from api.services.vaani.reply_sanitizer import ReplySanitizer
from api.services.vaani.state import CallState

ROOF = "మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?"
ROOF_SHORTER = "మీకు రూఫ్ లేదా టెర్రస్ ఉందా?"


def _filter(state) -> ReplyFilter:
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
    f._filler_state = None
    return f


def _state() -> CallState:
    s = CallState(required_fields=["roof_available", "monthly_bill"],
                  questions={"roof_available": ROOF,
                             "monthly_bill": "మీ కరెంట్ బిల్లు నెలకి ఎంత?"})
    return s


def test_run_898_a_third_ask_is_refused_once_the_budget_is_spent():
    s = _state()
    s.ask_counts["roof_available"] = s.MAX_ASKS_PER_FIELD
    assert "roof_available" not in s.still_need, "precondition: out of budget"
    assert "roof_available" not in s.known, "precondition: never answered"

    assert _filter(s)._gate(ROOF) == guardrails.REPAIR_LINE


def test_the_shortened_rewording_is_refused_too():
    """Run 898's third ask dropped a word. Wording is not the subject."""
    s = _state()
    s.ask_counts["roof_available"] = s.MAX_ASKS_PER_FIELD
    assert _filter(s)._gate(ROOF_SHORTER) == guardrails.REPAIR_LINE


def test_the_second_ask_is_still_allowed():
    """Two is the budget. One clarification is the whole point of it."""
    s = _state()
    s.ask_counts["roof_available"] = 1
    assert _filter(s)._gate(ROOF) == ROOF


def test_the_first_ask_is_untouched():
    s = _state()
    assert _filter(s)._gate(ROOF) == ROOF


def test_a_field_still_in_budget_and_unknown_is_untouched():
    s = _state()
    s.ask_counts["roof_available"] = s.MAX_ASKS_PER_FIELD
    bill = "మీ కరెంట్ బిల్లు నెలకి ఎంత?"
    assert _filter(s)._gate(bill) == bill
