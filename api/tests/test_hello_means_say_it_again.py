"""Run 1031 (23 Sep): every "హలో" cost the caller a question he never heard.

    BOT  మీది సొంత ఇల్లా,            cut off mid-question (property)
    USER హలో.
    BOT  మీ కరెంట్ బిల్లు నెలకి        jumped to the BILL
    USER హలో.
    USER హలో.
    BOT  సరే, మీరు ఏ ఏరియా...        jumped to AREA
    BOT  సరే, మీకు సొంత రూఫ్...       jumped to ROOF

Twelve turns, `end_call`, and every extracted field null.

"హలో" is in `barge_in.INTERRUPT_WORDS` deliberately, and that half is right --
the file says so: a caller saying hello while the bot is talking "is a caller
who cannot hear it. Continuing to talk over them is the worst possible
response." 381 occurrences in the logs, three times the next most common thing
anyone says to this agent.

What was missing is the other half. Stopping is not enough; the caller has to
hear the question he interrupted. Triage refunds an ask for "ఆగండి" (stop) via
WANTS_THE_FLOOR, but measured against the real regexes:

    'ఆగండి'        wants_the_floor True
    'హలో'          wants_the_floor False
    'ఏమన్నారు'      wants_the_floor False
    'అర్థం కాలేదు'   wants_the_floor False

So the three phrases that literally mean "say that again" refunded nothing. The
ask was gone, the field left `still_need`, and the agent moved on to a question
the caller had even less chance of hearing.

These are the OPPOSITE intent to the rest of INTERRUPT_WORDS and must not be
treated alike. "ఆగండి"/"వద్దు" mean *stop talking*; "హలో"/"ఏమన్నారు" mean
*repeat yourself*. Both stop the bot. Only one of them should bring the question
back.

Bounded by `MAX_REFUNDS_PER_FIELD` exactly as every other refund is, so a caller
on a genuinely bad line cannot turn this into run 870's unbounded loop.
"""

from api.services.vaani import triage
from api.services.vaani.state import CallState

QUESTIONS = {
    "property_type": "మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?",
    "monthly_bill": "మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?",
    "location": "మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?",
}

CHANNEL_FAILURES = ["హలో", "హలో అండి", "హలో?", "ఏమన్నారు", "ఏమన్నారండి",
                    "అర్థం కాలేదు", "ఏంటండి", "hello"]


def _asked(field="property_type"):
    """A call where `field` has just been asked once."""
    state = CallState(required_fields=list(QUESTIONS), questions=dict(QUESTIONS))
    state.ask_counts[field] = 1
    state.last_asked = field
    return state


def test_hello_gives_the_question_back():
    state = _asked()
    triage.apply(state, "హలో")
    assert state.ask_counts.get("property_type", 0) == 0, (
        "he never heard the question; the ask must be refunded")


def test_hello_puts_the_field_back_on_the_checklist():
    state = _asked()
    triage.apply(state, "హలో")
    assert "property_type" in state.still_need, (
        "run 1031 lost the field entirely and saved a null")


def test_every_channel_failure_phrase_refunds():
    missed = []
    for text in CHANNEL_FAILURES:
        state = _asked()
        triage.apply(state, text)
        if state.ask_counts.get("property_type", 0) != 0:
            missed.append(text)
    assert not missed, f"these mean 'say it again' and refunded nothing: {missed}"


def test_it_asks_the_same_question_again_not_the_next_one():
    """A refund alone is not enough -- `still_need` order decides what is
    nominated, and the whole failure was moving on to a DIFFERENT field."""
    state = _asked()
    triage.apply(state, "హలో")
    assert state.still_need[0] == "property_type"


def test_stop_is_not_treated_as_a_repeat_request():
    """"ఆగండి" already refunds through WANTS_THE_FLOOR. This is only here so a
    later edit cannot collapse the two categories -- they are opposite intents
    and the distinction is the whole point of this file."""
    state = _asked()
    triage.apply(state, "ఆగండి")
    assert state.ask_counts.get("property_type", 0) == 0


def test_an_ordinary_answer_does_not_refund():
    """The guard must not fire on normal speech, or the budget means nothing."""
    state = _asked()
    triage.apply(state, "సొంత ఇల్లు అండి")
    assert state.ask_counts.get("property_type", 0) == 1


def test_the_refund_is_bounded():
    """Run 870 was an unbounded loop built out of refunds. A caller on a dead
    line saying హలో forever must not earn an infinite budget."""
    state = _asked()
    for _ in range(12):
        state.ask_counts["property_type"] = 1
        state.last_asked = "property_type"
        triage.apply(state, "హలో")
    refunds = getattr(state, "refunds", {}) or {}
    assert refunds.get("property_type", 0) <= triage.MAX_REFUNDS_PER_FIELD


def test_hello_is_not_counted_as_an_answer():
    """The SECOND door on run 1031, closed 24 Sep.

    Refunding the ASK (above) was one of two per-field budgets. The other is
    `answer_counts`, charged by `note_answer_to_last_ask`, and `still_need`
    retires a field at MAX_ANSWERS_PER_FIELD with no refund path. "హలో" is
    neither a question nor a pure hesitation, so it used to be counted as an
    answer -- and a caller who could not hear lost the field through that door
    instead.
    """
    state = _asked()
    state.note_answer_to_last_ask("హలో")
    assert state.answer_counts.get("property_type", 0) == 0
    assert "property_type" not in state.answered_pending


def test_the_real_run_1031_shape_keeps_every_budget():
    """One "హలో" per question, then the re-ask -- how run 1031 actually went.
    Each field must come out with its full budget intact for the real answer."""
    state = CallState(required_fields=list(QUESTIONS), questions=dict(QUESTIONS))
    for field in QUESTIONS:
        state.ask_counts[field] = state.ask_counts.get(field, 0) + 1
        state.last_asked = field
        triage.apply(state, "హలో")
        state.note_answer_to_last_ask("హలో")
        state.ask_counts[field] = state.ask_counts.get(field, 0) + 1
        state.last_asked = field
        assert state.answer_counts.get(field, 0) == 0, field
        assert field in state.still_need, (
            f"{field} was retired after one హలో; the caller never heard it")
