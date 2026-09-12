"""Run 899: the roof was fixed and the fault moved to whatever he answered least clearly.

    property_type  3 asks      location  3 asks      captured 1/6

Run 898 was roof 4 / survey 3. Run 897 was bill 4 / location 3. Every call the
cap binds on most fields and fails on one or two -- a different one or two each
time -- and the calls with the most overruns are the calls with the most double
replies. Run 899 had three.

That is the coupling. `commit_ask` is charged at `LLMFullResponseEndFrame`, and
this file already records the reason: "A reply cut off before it ends is now not
charged at all, which is the right answer on its own terms: the caller never
heard the whole question." When two caller finals produce two overlapping
replies, one of them does not reach that frame, so its question is never
charged -- and an uncharged ask is an ask the budget cannot see. `last_asked` is
set there too, so `answered_pending` loses its footing on the same turns.

Fixing the overlap means changing turn-taking, which cost three calls and is
out of bounds. So the ask is charged where it becomes real to the caller
instead: the moment the question is handed to the speech engine.

The trade is named rather than hidden. A question cut off mid-word now counts
against the budget, which run 783 argued against -- the caller never heard the
whole thing. Two asks is a generous budget, and being asked the same question
four times is the complaint actually on the table.
"""

from api.services.vaani import guardrails
from api.services.vaani.brain_processor import ReplyFilter
from api.services.vaani.reply_sanitizer import ReplySanitizer
from api.services.vaani.state import CallState

ROOF = "మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?"
PROPERTY = "మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?"


def _state() -> CallState:
    return CallState(required_fields=["property_type", "roof_available"],
                     questions={"property_type": PROPERTY,
                                "roof_available": ROOF})


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


def test_the_question_is_charged_when_it_goes_out_not_when_the_reply_ends():
    s = _state()
    _filter(s)._gate(PROPERTY)
    assert s.ask_counts.get("property_type") == 1
    assert s.last_asked == "property_type"


def test_a_reply_that_never_finishes_still_spends_its_ask():
    """The overlap case. No End frame, so `commit_ask` never runs."""
    s = _state()
    for _ in range(2):
        f = _filter(s)
        f._gate(PROPERTY)          # spoken, then overtaken: no completion
        s.end_user_turn()
    assert s.ask_counts["property_type"] == 2
    assert "property_type" not in s.still_need
    # And the third is refused by the spent-budget guard.
    assert _filter(s)._gate(PROPERTY) == guardrails.REPAIR_LINE


def test_it_is_not_charged_twice_for_one_question():
    """`_gate` raises the flag; the completion path reads it and stands down.

    The de-duplication lives at the two CALL SITES, not inside `commit_ask`.
    Putting it inside changed that method's contract and broke seven existing
    suites, which is how this landed here instead.
    """
    s = _state()
    _filter(s)._gate(PROPERTY)
    assert s.ask_counts["property_type"] == 1
    assert s.ask_charged_on_air is True, (
        "the completion path has nothing to tell it the question was charged")

    # And the flag lasts exactly one turn.
    s.end_user_turn()
    assert s.ask_charged_on_air is False


def test_two_different_fields_in_one_turn_are_both_charged():
    s = _state()
    f = _filter(s)
    f._gate(PROPERTY)
    f._spoken = ""                 # a second question on a later turn
    s.end_user_turn()
    _filter(s)._gate(ROOF)
    assert s.ask_counts.get("property_type") == 1
    assert s.ask_counts.get("roof_available") == 1


def test_a_sentence_that_asks_for_nothing_is_charged_nothing():
    s = _state()
    _filter(s)._gate("థాంక్యూ అండి.")
    assert s.ask_counts == {}


def test_the_completion_path_still_works_on_its_own():
    """Text chat builds one pipeline per message and has no start frame."""
    s = _state()
    s.pending_ask = "roof_available"
    s.commit_ask(ROOF)
    assert s.ask_counts.get("roof_available") == 1
