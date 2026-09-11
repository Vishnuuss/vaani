"""Run 882: he asked what solar costs, and was asked what time suited him.

    USER: పేయించుకుంటాను కానీ. బ్యాలెన్స్ అంటే కాస్ట్ ఎంత వేసింది మామూలుగా?
          60 టు 60 స్క్వేర్ ఫీట్ అయితే
    USER: హలో.                                    <- checking anyone is there
    BOT : సరే అండి, సైట్ సర్వే కోసం మీరు ఏ సమయం కుదురుతుంది?

He asked again, and only then got an answer. Later:

    USER: సరే. మీరు ఎక్కడి నుంచి?
    USER: హలో.
    BOT : పర్వాలేదు అండి. మీకు కావాలంటే ఎప్పుడైనా కాల్ చేయండి. థాంక్యూ.   *** ended ***

The answer-first machinery is not missing and it is not weak. `render()` has a
whole branch for it -- the checklist is WITHDRAWN for a turn so that a list of
fields at the end of the context cannot out-rank the instruction to answer
(run 218's lesson, paid for over 36 turns). It simply never ran.

It is gated on `_is_question(self.last_user_text)`, and `last_user_text` was
assigned per TRANSCRIPTION, not per turn:

    self.state.last_user_text = text.strip()

A caller's turn routinely arrives as several transcriptions -- a sentence, a
pause, then "హలో" when nothing comes back. The last fragment overwrites the
question, `_is_question("హలో.")` is False, the checklist returns, and the agent
asks the next field. The more impatient he gets, the more reliably his question
is erased, which is exactly backwards.

So the caller's turn is accumulated and judged whole.
"""

from __future__ import annotations

from api.services.vaani.state import CallState, _is_question


def _state() -> CallState:
    s = CallState()
    s.required_fields = ["monthly_bill", "customer_name"]
    s.questions = {f: f"tell me your {f}" for f in s.required_fields}
    return s


def test_a_question_is_not_erased_by_a_following_hello():
    s = _state()
    s.note_user_said("బ్యాలెన్స్ అంటే కాస్ట్ ఎంత వేసింది మామూలుగా?")
    s.note_user_said("హలో.")
    assert _is_question(s.last_user_text), (
        "run 882: the question was wiped by the 'hello' he said when nothing "
        "came back, so the answer-first branch never fired and he was asked "
        "what time suited him instead")


def test_the_answer_first_branch_actually_fires_for_that_turn():
    s = _state()
    s.note_user_said("కాస్ట్ ఎంత అవుతుంది?")
    s.note_user_said("హలో.")
    assert "THE CALLER ASKED YOU SOMETHING" in s.render(), (
        "the checklist must be withdrawn for this turn -- prose under a list "
        "of fields does not beat the list (run 218)")


def test_the_turn_resets_once_the_agent_has_replied():
    s = _state()
    s.note_user_said("కాస్ట్ ఎంత అవుతుంది?")
    s.end_user_turn()
    s.note_user_said("హైదరాబాద్.")
    assert not _is_question(s.last_user_text), (
        "a question from a turn already answered must not suppress the "
        "checklist for ever")
    assert s.last_user_text == "హైదరాబాద్."


def test_fragments_of_one_turn_count_as_ONE_answer():
    """`answer_counts` must not be spent three times on one sentence."""
    s = _state()
    s.pending_ask = "monthly_bill"
    s.commit_ask()
    # Both calls, in the order `ReplyFilter.note_user_text` makes them.
    for fragment in ("మాది వచ్చేసింది.", "80 టు 90 థౌసండ్ వస్తుంది."):
        s.note_user_said(fragment)
        s.note_answer_to_last_ask(fragment)
    assert s.answer_counts.get("monthly_bill") == 1, (
        "run 882 delivered that answer as two transcriptions. Counting each "
        "one retires the field after a single real answer")


def test_the_whole_turn_is_what_gets_acknowledged():
    s = _state()
    s.note_user_said("మాది వచ్చేసింది.")
    s.note_user_said("80 టు 90 థౌసండ్ వస్తుంది.")
    assert "80" in s.last_user_text and "వచ్చేసింది" in s.last_user_text, (
        "the state block acknowledges `last_user_text`; half a turn is a "
        "half-acknowledgement")
