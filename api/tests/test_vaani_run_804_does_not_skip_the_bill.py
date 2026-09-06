"""Run 804: the caller was cut off mid-answer and lost the question forever.

The call, 6 Sep, verbatim from the log:

    BOT : మీ నెల బిల్లు ఎంత వస్తుంది?        ask #1
    USER: వచ్చేసి                             -- still assembling the sentence
    BOT : మీరు ఏ ఏరియాలో ఉంటున్నారు?         MOVED ON
    USER: ఏం బిల్ చెప్పలా ఏదో                 -- "I didn't say the bill"
    BOT : సరే, మీ కరెంట్ బిల్లు నెలకి ఎంత?     ask #2
    USER: నేను చెప్పలే కదా, ఎందుకు ముందుకు పోతున్నావ్?
    BOT : మీకు సొంత రూఫ్ ఉందా?                MOVED ON -- bill abandoned
    USER: బిల్లు తెలుసుకోలేదు కదా, మరి నెక్స్ట్ క్వశ్చన్ ఏమి పోయినారు?

Saved lead: monthly_bill=null, location=null, customer_name=null. Three of six
fields lost, and the caller told the agent what it was doing wrong twice.

`MAX_ASKS_PER_FIELD = 2` is not the bug -- run 218 earned it, and a caller asked
the same thing four times hangs up. The bug is WHAT COUNTS AS AN ASK:

  * "వచ్చేసి" is a fragment. `completeness.sounds_unfinished` returns True for
    it, so the system KNEW the caller had not finished. The turn was ended
    anyway, and then charged to the field as though it had been answered and
    declined. An ask the caller never got to answer is not an ask.

  * The budget is two. Spend one on an interruption and the caller gets a single
    real attempt; spend two and the field leaves `still_need` and can never be
    nominated again, however loudly he asks for it.

So the field must survive an interrupted turn, and a caller who says "I haven't
told you yet" must be able to get the question back.
"""

from __future__ import annotations

from api.services.vaani import triage
from api.services.vaani.state import CallState

FIELDS = ["property_type", "monthly_bill", "location",
          "roof_available", "customer_name", "assessment_agreed"]

QUESTIONS = {
    "property_type": "మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?",
    "monthly_bill": "మీ నెల బిల్లు ఎంత వస్తుంది?",
    "location": "మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?",
    "roof_available": "మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?",
    "customer_name": "మీ పేరు చెప్పగలరా?",
    "assessment_agreed": "ఉచితంగా ఒక సైట్ సర్వే చేయించుకుంటారా?",
}


def _state() -> CallState:
    return CallState(required_fields=list(FIELDS), questions=dict(QUESTIONS))


def _ask(state: CallState, field: str, caller_said: str) -> None:
    """One full exchange, in the order production runs it.

    `render()` nominates the field, `commit_ask()` charges it when the agent
    finishes speaking, and only then does the caller reply and triage run. The
    refund therefore always arrives one step after the charge, which is exactly
    why the charge alone cannot be the place to fix this.
    """
    state.pending_ask = field
    state.commit_ask()
    triage.apply(state, caller_said)
    state.last_user_text = caller_said


def test_a_fragment_does_not_spend_one_of_the_two_asks():
    """"వచ్చేసి" is the caller starting to answer, not declining to."""
    state = _state()
    _ask(state, "monthly_bill", "వచ్చేసి")

    assert state.ask_counts.get("monthly_bill", 0) == 0, (
        "the caller was interrupted mid-answer and still had the question "
        "charged against him"
    )
    assert "monthly_bill" in state.still_need


def test_the_bill_is_still_askable_after_run_804s_two_interruptions():
    """The whole of run 804, replayed. The bill must survive it."""
    state = _state()

    # Turn 1: cut off mid-answer.
    _ask(state, "monthly_bill", "వచ్చేసి")
    # Turn 2: he points out he never answered.
    state.last_user_text = "ఏం బిల్ చెప్పలా ఏదో."
    # Turn 3: asked again, and he protests rather than answering.
    _ask(state, "monthly_bill", "నేను చెప్పలే కదా ఎందుకు ఎక్స్‌ప్రెషన్ పోతున్నావ్ మీరు")

    assert "monthly_bill" not in state.abandoned, (
        "run 804: the bill was abandoned while the caller was actively asking "
        "to be asked it"
    )
    assert "monthly_bill" in state.still_need


def test_saying_you_have_not_answered_yet_gives_the_question_back():
    """The caller's own words are the repair signal; nothing else is needed."""
    state = _state()
    _ask(state, "monthly_bill", "ఒక లక్ష")          # a real answer, ask spent
    _ask(state, "monthly_bill", "ఒక లక్ష అండి")      # asked again, spent again
    assert "monthly_bill" in state.abandoned         # the cap has bitten

    # "I didn't tell you, did I" -- run 804's exact complaint.
    triage.apply(state, "బిల్లు తెలుసుకోలేదు కదా, మరి నెక్స్ట్ క్వశ్చన్ ఏమి పోయినారు?")

    assert "monthly_bill" not in state.abandoned
    assert "monthly_bill" in state.still_need


def test_a_complete_answer_still_spends_its_ask():
    """The cap must keep working; run 218's caller hung up because it did not."""
    state = _state()
    _ask(state, "monthly_bill", "నెలకి రెండు లక్షలు వస్తుంది")
    assert state.ask_counts["monthly_bill"] == 1

    _ask(state, "monthly_bill", "రెండు లక్షలు అని చెప్పాను కదా")
    assert state.ask_counts["monthly_bill"] == 2
    assert "monthly_bill" in state.abandoned
