"""The call must end when it is over, and only then.

Audit of runs 1013-1044 (24 Sep): the call failed to end, or said its goodbye
two to four times, in 8 of 17 real conversations -- and in ALL THREE calls on
the build that was live that evening. Four callers had to ask the agent to hang
up: "కట్ చేయండి" (1020), "కట్ చేయండి, ఇక" (1021), "హలో, కట్ చేయరా మీరు?" (1036),
and run 1044, where the booking line was spoken three times and the caller hung
up himself after "లేదు, కట్ చేయట్లేదా?".

Traced through the code, the chain in run 1044:

  1. "ఆ, చేయించుకుంటాను" -- yes, I will get the survey -- never counted as
     agreement. `next_step_agreed` is set only by `AGREED`, which needs a TIME
     word. A plain yes to the booking question could not match.
  2. So `closing_is_due()` stayed False, so no goodbye was counted, so the
     `closings_said >= 1` escalation to `must_end` could never fire.
  3. The caller's own "కట్ చేయండి." did not match FAREWELL: the pattern read
     `కట్\\s*చేస్(తారా|తా|ేయండి)` -- the third branch spells "చేస్ేయండి", which is
     not a word. Only "ఫోన్ కట్ ..." worked, through the other branch.
  4. The repeated booking line was never caught, because the repeat guard
     decided on a 1-4 character sliver (HOLDBACK releases `buffer[:-24]`),
     `_looks_like_repeat` refuses anything under 6, and the check never runs
     again once anything has been spoken.

And one trap under the obvious fix: setting `next_step_agreed` alone makes
`must_close` True, which blocks the slot OFFER (it contains a "?") and swaps in
SAFE_CLOSE -- so the caller who just said yes would be hung up on without a
time. The fix is `guardrails.is_time_offer`: after a yes and before a time is
fixed, exactly one kind of question survives -- one that names a time. A first
draft relaxed `must_close` itself and let ANY question through after a yes; the
existing test_a_question_after_the_call_is_won_is_replaced caught that.
"""

from api.services.vaani import guardrails, triage
from api.services.vaani.state import CallState

FIELDS = {
    "property_type": "మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?",
    "customer_name": "మీ పేరు చెప్పగలరా?",
    "assessment_agreed": "ఉచితంగా ఒక సైట్ సర్వే చేయించుకుంటారా?",
}


def _state(asked: str = "") -> CallState:
    s = CallState(required_fields=list(FIELDS), questions=dict(FIELDS))
    if asked:
        s.ask_counts[asked] = 1
        s.last_asked = asked
    return s


# --- 3. the caller's own "cut the call" -------------------------------------
def test_run_1044_cut_the_call_ends_it():
    for said in ("కట్ చేయండి.", "కట్ చేయరా మీరు?", "కట్ చేయట్లేదా?",
                 "హలో, కట్ చేయరా మీరు?", "కట్ చేయండి, ఇక"):
        s = _state()
        triage.apply(s, said)
        assert s.must_end, f"{said!r} is the caller asking us to hang up"


def test_the_old_branches_still_work():
    for said in ("ఫోన్ కట్ చేయండి", "కాల్ కట్ చేస్తారా", "కట్ చేస్తా"):
        s = _state()
        triage.apply(s, said)
        assert s.must_end, said


def test_a_word_that_merely_starts_with_cut_does_not_hang_up():
    """కట్టాలి -- "have to pay/tie" -- must not end a sales call."""
    for said in ("కట్టాలి", "బిల్లు కట్టాను", "ఎంత కట్టాలి?"):
        s = _state()
        triage.apply(s, said)
        assert not s.must_end, said


# --- 1. a yes to the booking question is agreement --------------------------
def test_run_1044_yes_to_the_survey_is_agreement():
    s = _state(asked="assessment_agreed")
    triage.apply(s, "ఆ, చేయించుకుంటాను")
    assert s.next_step_agreed


def test_every_common_yes_counts():
    for said in ("అవును", "సరే అండి", "ఓకే", "తప్పకుండా", "చేయించుకుంటాను",
                 "ఆ, చేయించుకుంటా"):
        s = _state(asked="assessment_agreed")
        triage.apply(s, said)
        assert s.next_step_agreed, said


def test_a_yes_to_a_different_question_is_not_agreement():
    """The booking field is what makes a yes an agreement."""
    s = _state(asked="property_type")
    triage.apply(s, "అవును")
    assert not s.next_step_agreed


def test_a_no_or_a_later_to_the_survey_is_not_agreement():
    for said in ("వద్దు", "ఇప్పుడు కాదు, తర్వాత చూద్దాం", "లేదు అండి"):
        s = _state(asked="assessment_agreed")
        triage.apply(s, said)
        assert not s.next_step_agreed, said


# --- the trap: agreed with no time yet must still be OFFERED a time ---------
OFFER = "రేపు ఉదయం ten o'clock లేదా సాయంత్రం four o'clock, ఏది కుదురుతుంది అండి?"
QUALIFY = "సరే అండి. మీ ఇంటి రూఫ్ ఎంత ఉంది?"


def test_agreed_without_a_time_may_still_be_offered_one():
    """The trap: the offer contains a '?', and a blanket must_close swaps it for
    SAFE_CLOSE -- hanging up on a caller who just said yes, with no time."""
    s = _state(asked="assessment_agreed")
    s.next_step_agreed = True
    assert guardrails.is_time_offer(OFFER, s)


def test_but_no_other_question_gets_through_after_agreement():
    """A first draft of this fix relaxed must_close itself, which let ANY
    question through once he agreed -- test_reply_sanitizer's
    test_a_question_after_the_call_is_won_is_replaced caught it. Only the time
    offer is exempt; a qualifying question after a yes is still refused."""
    s = _state(asked="assessment_agreed")
    s.next_step_agreed = True
    assert guardrails.must_close(s)
    assert not guardrails.is_time_offer(QUALIFY, s)


def test_once_a_time_is_fixed_nothing_more_is_offered():
    s = _state()
    s.next_step_agreed = True
    s.appointment_iso = "2026-09-25T10:00"
    assert guardrails.must_close(s)
    assert not guardrails.is_time_offer(OFFER, s)
