"""Run 880: he said he was interested, and the agent hung up on him.

    BOT : మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?
    USER: యాక్చువల్గా మాకు ఈ సోలార్ అయితే ఇంట్రెస్ట్ ఉంది కానీ ఇప్పుడైతే
          ఆ అంటే నాకు కొంచెం ఐడియా లేదు దాని గురించి
          హలో.
    BOT : పర్వాలేదు అండి. మీకు కావాలంటే ఎప్పుడైనా కాల్ చేయండి. థాంక్యూ.
                                                        *** hung up, 32s ***

"We ARE interested in solar, but right now I don't know much about it" is a
request to be told about it. It is the easiest lead on the list. Six fields were
still unasked and he had refused nothing.

Nothing deterministic did this -- every triage pattern was run against that exact
sentence and not one matches. The model emitted `MODE: END` and `_note_mode`
set `must_end` with no questions asked, because nothing ever checked whether
ending was EARNED. The same close appears in runs 866 and 876, so this is not
new; it has simply never been looked at.

Hanging up is a business action, and the brief this work follows is explicit
that the model does not own those: it owns language, the application owns
truth. A wrong word costs a sentence. A wrong hangup costs the lead.

So `MODE: END` is now a REQUEST. It is honoured when the call has a reason to be
over -- he refused, he is disqualified, the booking is made, the checklist is
done, or the state has already decided. Otherwise it is ignored and the agent
keeps talking to the man who said he was interested.
"""

from __future__ import annotations

import pytest

from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from api.services.vaani.brain_processor import ReplyFilter
from api.services.vaani.state import CallState


class _Injector:
    def __init__(self, state):
        self.state = state


def _filter(state):
    rf = ReplyFilter(_Injector(state))

    async def _sink(frame, direction=FrameDirection.DOWNSTREAM):
        return None

    rf.push_frame = _sink
    return rf


def _fresh_call() -> CallState:
    """Six fields unasked, nothing refused. Run 880's exact position."""
    s = CallState()
    s.required_fields = ["property_type", "monthly_bill", "location",
                         "roof_available", "customer_name", "assessment_agreed"]
    s.questions = {f: f"tell me your {f}" for f in s.required_fields}
    return s


async def _say(rf, text):
    await rf.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await rf.process_frame(LLMTextFrame(text), FrameDirection.DOWNSTREAM)
    await rf.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)


@pytest.mark.asyncio
async def test_the_model_may_not_hang_up_on_a_lead_who_refused_nothing():
    s = _fresh_call()
    rf = _filter(s)
    await _say(rf, "MODE: END\nపర్వాలేదు అండి. మీకు కావాలంటే ఎప్పుడైనా కాల్ చేయండి.")
    assert not s.must_end, (
        "he said he was INTERESTED and had refused nothing, with six fields "
        "still unasked. Run 880 hung up on him at 32 seconds")


@pytest.mark.asyncio
async def test_a_refusal_still_ends_the_call():
    s = _fresh_call()
    s.refusals = 2
    rf = _filter(s)
    await _say(rf, "MODE: END\nసరే అండి, థాంక్యూ.")
    assert s.must_end, "a man who has said no must be let go"


@pytest.mark.asyncio
async def test_a_disqualified_caller_still_ends_the_call():
    s = _fresh_call()
    s.disqualified = True
    rf = _filter(s)
    await _say(rf, "MODE: END\nసరే అండి, థాంక్యూ.")
    assert s.must_end


@pytest.mark.asyncio
async def test_a_booked_appointment_still_ends_the_call():
    s = _fresh_call()
    s.appointment_iso = "2026-09-12T10:00:00+05:30"
    rf = _filter(s)
    await _say(rf, "MODE: END\nసరే అండి, రేపు ten o'clock. థాంక్యూ.")
    assert s.must_end, "the business is done; keeping him on the line is rude"


@pytest.mark.asyncio
async def test_an_empty_checklist_still_ends_the_call():
    s = _fresh_call()
    s.required_fields = []
    rf = _filter(s)
    await _say(rf, "MODE: END\nసరే అండి, థాంక్యూ.")
    assert s.must_end


@pytest.mark.asyncio
async def test_a_state_that_already_decided_is_not_second_guessed():
    """`must_end` set by triage or by the second-closing rule is authoritative."""
    s = _fresh_call()
    s.must_end = True
    rf = _filter(s)
    await _say(rf, "MODE: END\nసరే అండి, థాంక్యూ.")
    assert s.must_end
