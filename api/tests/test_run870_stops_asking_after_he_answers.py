"""Run 870: he answered four times and was asked a fifth. Then he hung up.

    bot : మీరు ఉంటున్న ఇల్లు సొంతమా అండి, లేక rent కి ఉంటున్నారా?
    user: ఆ సంద మేమ్.                                    (garbled by STT)
    bot : మంచిది. మీ ఇంటికి solar పెట్టించుకోవాలని ...?
    user: చూడడానికి ఆలోచన అయితే ఉంది.                     (yes, I am thinking about it)
    bot : సరే. మీరు మీ ఇంటి యజమాని అండి, లేక అద్దెకు ఉంటున్నారా?
    user: పాయి సంతీల్ అన్నాము వాళ్ళ అజమాని అని అడుగుతారు.  (garbled)
    bot : మంచిది. మీ ఇంటిలో solar పెట్టుకోవాలని ...?
    user: చూపుతున్నామని చెప్పాను.                          (I SAID I am interested)
    bot : మీ ఇంటి యజమాని మీరు తానేనా, లేక ఇంకెవరో ...?
    user: నేను ఎన్నిసార్లు చెప్పాలి?                        (how many times must I say it)
    bot : మీ ఇంటి యజమాని మీరు తానేనా, లేక ఇంకెవరో ...?      <- asked AGAIN
    user: నేను నేను నేను నేను నేను.
    bot : ... మా executive వచ్చే రోజు మీకు బుక్ చేసుకోవచ్చా?
    user: చెత్త చెత్త మాట్లాడొద్దు కదా.                     (stop talking rubbish)
                                                            *** hung up ***

`answered_pending` (run 853) suppresses a field for exactly ONE turn while the
async extractor catches up. That is right when the extractor eventually gets a
value. Here it never did: every one of his answers came back from Sarvam as
mush -- "ఆ సంద మేమ్", "పాయి సంతీల్ అన్నాము" -- so `known` stayed empty, the
field returned to STILL_NEED every other turn, and the loop had no end.

MAX_ASKS_PER_FIELD did not bound it either. It counts ASKS, and the asks
alternate between two fields, so neither reached its cap while the caller was
answering both.

The missing invariant is not about asking. It is this:

    A man who has ANSWERED twice must not be asked a third time,
    whether or not we managed to understand him.

Understanding is the extractor's job and it is allowed to fail. Asking a human
being the same question five times is not a degraded answer, it is an insult,
and it is the thing that ended this call.
"""

from __future__ import annotations

from api.services.vaani.state import CallState


def _state() -> CallState:
    s = CallState()
    s.required_fields = ["house_ownership", "solar_planning"]
    s.questions = {f: f"tell me your {f}" for f in s.required_fields}
    # The ASK budget is deliberately neutralised in these tests.
    #
    # It is not what failed. In production it is refunded by `_refund_ask` and
    # re-issued by `misheard_last_turn`, which is exactly how run 870 got to a
    # fourth ask -- so a test that leaves it at 2 passes without the fix, for
    # the wrong reason, and proves nothing. Setting it out of the way isolates
    # the counter this file is about: HIS answers.
    s.MAX_ASKS_PER_FIELD = 99
    return s


def _answer(s: CallState, field: str, text: str) -> None:
    """One full turn: we asked `field`, he said `text`, we replied.

    `end_user_turn` is what `ReplyFilter` calls once a reply is out, and it is
    what makes the NEXT utterance a new turn. Without it every fragment after
    the first is treated as more of the same turn and charged nothing -- which
    is the point of `counted_this_turn`, and why a turn has to be ended here
    rather than left open (run 882).
    """
    s.pending_ask = field
    s.commit_ask()
    s.note_user_said(text)
    s.note_answer_to_last_ask(text)
    s.end_user_turn()


def test_two_substantive_answers_retire_the_field_even_with_no_value():
    s = _state()
    _answer(s, "house_ownership", "ఆ సంద మేమ్")
    # `answered_pending` suppresses it for exactly this turn (run 853). The
    # next turn clears that, and with nothing extracted the field comes back --
    # which is correct, and is where run 870's loop began.
    _answer(s, "solar_planning", "చూడడానికి ఆలోచన అయితే ఉంది")
    assert "house_ownership" in s.still_need, (
        "one garbled answer is not enough to retire a field -- he may really "
        "have been interrupted, and asking once more is reasonable")

    _answer(s, "house_ownership", "పాయి సంతీల్ అన్నాము వాళ్ళ అజమాని అని")
    assert "house_ownership" not in s.still_need, (
        "he answered twice. Sarvam garbled both. The value is unknown and that "
        "is acceptable -- asking him a third time is not")
    assert not s.known.get("house_ownership"), (
        "nothing was understood, so nothing may be invented into the lead")


def test_the_raw_answers_are_kept_even_when_nothing_was_extracted():
    s = _state()
    _answer(s, "house_ownership", "ఆ సంద మేమ్")
    _answer(s, "house_ownership", "పాయి సంతీల్ అన్నాము")
    assert s.heard.get("house_ownership") == [
        "ఆ సంద మేమ్", "పాయి సంతీల్ అన్నాము"], (
        "he said something twice. Whatever it was, a human reading this lead "
        "later should see it rather than a null")


def test_backchannels_and_questions_do_not_count_as_answers():
    s = _state()
    _answer(s, "house_ownership", "ఆ")
    _answer(s, "house_ownership", "ఆ")
    # Clear the one-turn `answered_pending` suppression so we are reading the
    # answer budget and not run 853's mechanism.
    _answer(s, "solar_planning", "ఆలోచన ఉంది")
    assert s.answer_counts.get("house_ownership", 0) == 0, (
        "'ఆ' is a thinking noise. Counting it as an answer would retire a "
        "question nobody ever answered and store a null for it")
    assert "house_ownership" in s.still_need

    s2 = _state()
    _answer(s2, "house_ownership", "ఎందుకు అడుగుతున్నారు?")
    _answer(s2, "house_ownership", "ధర ఎంత అవుతుంది?")
    assert "house_ownership" in s2.still_need, (
        "he asked US something both times. The question is still open")


def test_alternating_fields_still_retire_independently():
    """Run 870's exact shape: two fields, answers alternating between them."""
    s = _state()
    _answer(s, "house_ownership", "ఆ సంద మేమ్")
    _answer(s, "solar_planning", "చూడడానికి ఆలోచన అయితే ఉంది")
    _answer(s, "house_ownership", "పాయి సంతీల్ అన్నాము")
    _answer(s, "solar_planning", "చూపుతున్నామని చెప్పాను")

    assert not s.still_need, (
        "both fields were answered twice. Run 870 asked a fifth question after "
        "exactly this sequence and the caller hung up")


def test_a_confirmed_value_still_retires_the_field_immediately():
    """The extractor working is still the happy path and must not regress."""
    s = _state()
    _answer(s, "house_ownership", "సొంత ఇల్లు అండి")
    s.known["house_ownership"] = "own"
    assert "house_ownership" not in s.still_need
