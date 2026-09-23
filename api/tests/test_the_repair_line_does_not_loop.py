"""Run 1023 (23 Sep): it said "I could not hear you" four times to a caller it
had heard perfectly.

    USER కొంత రూఫ్ ఉంది.                       roof answered, clearly
    BOT  క్షమించండి, సరిగ్గా వినిపించలేదు...
    USER ఆ, ఉందండి, ఉంది.                      answered again
    BOT  క్షమించండి, సరిగ్గా వినిపించలేదు...
    USER కొంత రూఫ్ ఉంది.                       a third time
    BOT  క్షమించండి, సరిగ్గా వినిపించలేదు...
    USER ఏం వినబడలేదండి మీకు, కొంత రీఫ్ ఉందని చెప్తున్నా నేను
         "Can you not hear? I am TELLING you there is some roof."
    ...
    BOT  క్షమించండి, సరిగ్గా వినిపించలేదు...    a fourth

`roof_available` was already `True` in `known`. So every time the model asked
about the roof again, the answered-field guard blocked it and substituted
REPAIR_LINE -- correctly refusing to re-ask, and then telling the caller a lie:
it had heard him, the transcripts are in the run record.

The guard is right to block. The substitution is wrong the second time, for two
reasons that compound:

  1. The caller is being asked to repeat something the agent has already
     written down, so repeating it cannot help and he knows it.
  2. The apology is identical every turn, which is the exact "it asked the same
     question four times word for word" failure REPAIR_LINE was introduced to
     cure. Run 96 produced "you told me nothing"; run 1023 produced a hang-up.

So the repair line is a ONE-SHOT. Once it has been said and the next thing the
model produces is still blockable, saying it again is worse than letting the
model speak -- a repeated question at least moves the call, and the ask budget
and `must_close` remain in force above it.
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
    return f, state


def _fresh_turn(f):
    """Reset only what a new TURN resets.

    `ReplyFilter` is built once per CALL -- `self._said` "lives on this object
    and survives the whole phone call", which is what lets `_is_repeat` see a
    repeat at all. An earlier version of this helper cleared `_said` too, which
    quietly disarmed the guard under test and made the test agree with whatever
    the code did.
    """
    f._spoken = ""
    f._blocked = False
    f._pending_repeat = ""
    return f


def test_the_first_block_still_says_it_could_not_hear():
    """The one-shot must not remove the behaviour, only stop it repeating."""
    f, _ = _filter({"roof_available": True})
    assert f._gate(QUESTIONS["roof_available"]) == guardrails.REPAIR_LINE


def test_the_second_block_in_a_row_does_not_repeat_the_apology():
    f, _ = _filter({"roof_available": True})
    assert f._gate(QUESTIONS["roof_available"]) == guardrails.REPAIR_LINE
    _fresh_turn(f)
    again = f._gate(QUESTIONS["roof_available"])
    assert again != guardrails.REPAIR_LINE, (
        "four identical apologies is what made run 1023 hang up")


def test_run_1023_never_says_it_four_times():
    f, _ = _filter({"roof_available": True, "assessment_agreed": True})
    said = []
    for _ in range(4):
        _fresh_turn(f)
        said.append(f._gate(QUESTIONS["roof_available"]))
    assert said.count(guardrails.REPAIR_LINE) <= 1, (
        f"REPAIR_LINE emitted {said.count(guardrails.REPAIR_LINE)} times; "
        "it is a one-shot")


def test_an_ordinary_reply_rearms_the_repair_line():
    """A caller who is being understood again should get the repair line if a
    LATER turn genuinely needs it. The one-shot is about consecutive blocks,
    not about spending it once per call."""
    f, _ = _filter({"roof_available": True})
    assert f._gate(QUESTIONS["roof_available"]) == guardrails.REPAIR_LINE
    _fresh_turn(f)
    line = QUESTIONS["location"]
    assert f._gate(line) == line          # an unknown field passes through
    _fresh_turn(f)
    assert f._gate(QUESTIONS["roof_available"]) == guardrails.REPAIR_LINE


def test_the_repeat_guard_is_one_shot_too():
    """The wording-repeat path emits the same line and loops the same way.

    `_said` is appended where a reply is SPOKEN (`self._said.append(self._spoken)`
    in the frame path), not inside `_gate`, so a unit test of the gate has to
    seed the history itself. Calling `_gate` twice and expecting it to remember
    tests nothing -- it was passing the line straight back and the assertion
    would have been measuring the absence of the guard, not the presence of the
    fix.
    """
    f, _ = _filter({})
    line = QUESTIONS["location"]
    assert f._gate(line) == line
    f._said.append(line)                  # as the frame path would have done

    _fresh_turn(f)
    assert f._gate(line) == guardrails.REPAIR_LINE
    _fresh_turn(f)
    assert f._gate(line) != guardrails.REPAIR_LINE
