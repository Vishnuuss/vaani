"""An interrupted question must be re-askable, and an apology is not an ask.

Run 790, live. The caller was cut off mid-answer; the agent then abandoned the
question it had been asking, and the saved lead had `location: null`.

The trap, traced end to end:

1. Barge-in cancels the generation. `LLMFullResponseEndFrame` is a ControlFrame
   and is flushed from the queue, so the End branch never runs and the
   interrupted question is NOT charged. That part is correct -- it is the run
   783 fix.
2. But the half-spoken fragment still lands in `_said` at the NEXT response
   start.
3. The model re-asks the same question. `_is_repeat` compares the new full
   question against that fragment.
4. If the caller cut in LATE the fragment is nearly the whole question, so the
   0.80 similarity bar is cleared, `_gate` blocks the reply and substitutes
   REPAIR_LINE.
5. **The repair line completes normally, so it DOES reach the End branch, and
   `commit_ask()` charges the field for a question the caller never heard.**

Two of those and the field is abandoned for the rest of the call. The agent
spends the caller's question budget apologising for its own interruption.

No test anywhere drove an interruption through ReplyFilter and then looked at
`_said`, `state.asked` and `ask_counts`, which is why this survived. This is
that test.
"""

from __future__ import annotations

import pytest

from pipecat.frames.frames import (
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from api.services.vaani import guardrails
from api.services.vaani.brain_processor import ReplyFilter
from api.services.vaani.state import CallState

QUESTION = "సరే, మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?"
# Where the caller cut in on run 790 -- most of the question was already out.
FRAGMENT = "సరే, మీరు ఏ ఏరియా లేదా సిటీలో"


class _Injector:
    def __init__(self, state):
        self.state = state


def _state() -> CallState:
    return CallState(required_fields=["location", "customer_name"],
                     questions={"location": QUESTION,
                                "customer_name": "మీ పేరు చెప్పగలరా?"})


def _filter(state) -> ReplyFilter:
    rf = ReplyFilter(_Injector(state))
    rf.push_frame = _noop
    return rf


async def _noop(frame, direction=None):
    return None


async def _speak(rf, text, *, interrupted_after=None):
    """Drive one reply through the filter, optionally cut off part-way."""
    await rf.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    emitted = text if interrupted_after is None else interrupted_after
    await rf.process_frame(LLMTextFrame(emitted), FrameDirection.DOWNSTREAM)
    if interrupted_after is None:
        await rf.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)


@pytest.mark.asyncio
async def test_an_apology_does_not_spend_the_callers_question():
    """The core defect. REPAIR_LINE is not an ask and must not be charged."""
    st = _state()
    st.render()                       # nominates location
    rf = _filter(st)

    # The repair line is what actually gets spoken when the guard fires.
    await _speak(rf, guardrails.REPAIR_LINE)

    assert st.ask_counts.get("location", 0) == 0, (
        "the agent charged the caller a question for apologising to him")
    assert "location" in st.still_need


@pytest.mark.asyncio
async def test_a_re_ask_after_an_interruption_is_not_a_repeat():
    """The caller never heard the whole question, so asking it is not repeating."""
    st = _state()
    st.render()
    rf = _filter(st)

    # Question 1: cut off part-way. No End frame -- the interruption ate it.
    await _speak(rf, QUESTION, interrupted_after=FRAGMENT)
    await rf.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

    # Same field is still needed, so the model asks it again.
    st.render()
    rf2_blocked = rf._is_repeat(QUESTION)
    assert not rf2_blocked, (
        "a question the caller never finished hearing was treated as a repeat, "
        "so he gets 'I could not hear you' instead of the question")


@pytest.mark.asyncio
async def test_the_interrupted_question_is_still_not_charged():
    """The run 783 fix must survive this change."""
    st = _state()
    st.render()
    rf = _filter(st)
    await _speak(rf, QUESTION, interrupted_after=FRAGMENT)
    assert st.ask_counts.get("location", 0) == 0
    assert "location" in st.still_need


@pytest.mark.asyncio
async def test_a_genuine_repeat_is_still_caught():
    """The guard must keep working -- run 721 asked one question four times."""
    st = _state()
    rf = _filter(st)
    await _speak(rf, QUESTION)                 # said in full
    assert rf._is_repeat(QUESTION), "a real verbatim repeat must still be caught"


@pytest.mark.asyncio
async def test_a_completed_question_is_still_charged():
    """The budget must still bite, or the agent can interrogate forever."""
    st = _state()
    st.render()
    rf = _filter(st)
    await _speak(rf, QUESTION)
    assert st.ask_counts.get("location") == 1
