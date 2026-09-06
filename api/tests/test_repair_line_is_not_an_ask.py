"""An apology must not be billed as one of the caller's two questions.

Run 783's trap, traced end to end and untested until now:

1. The caller barges in. `LLMFullResponseEndFrame` is a ControlFrame and gets
   flushed by the interruption, so the half-spoken question never reaches the
   End branch and is correctly NOT charged.
2. But the truncated fragment still lands in `ReplyFilter._said`.
3. The model re-asks the same question.
4. `_is_repeat` compares the new full question against that fragment. If the
   caller cut in LATE the fragment is nearly the whole question, the similarity
   ratio clears 0.80, and `_gate` substitutes REPAIR_LINE --
   "క్షమించండి, సరిగ్గా వినిపించలేదు".
5. **The repair line completes normally.** It reaches the End branch and charges
   the field for a question the caller never heard.

Two of those and the field is `abandoned`, never nominated again, saved null.
The agent spends the caller's question budget apologising for its own
interruption.

Why this file exists at all
---------------------------
This was fixed once and reverted, because the test guarding it asserted on
SOURCE TEXT -- `"_strategy" in src or "getattr" in src` -- and passed whether or
not the behaviour was there. A test that cannot fail is not a test. This one
drives frames through the real processor and reads the real counters.
"""

from __future__ import annotations

import pytest

from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from api.services.vaani import guardrails
from api.services.vaani.brain_processor import ReplyFilter
from api.services.vaani.state import CallState


class _Injector:
    def __init__(self, state):
        self.state = state


def _filter():
    st = CallState(required_fields=["location"],
                   questions={"location": "మీరు ఏ ఏరియాలో ఉంటున్నారు?"})
    rf = ReplyFilter(_Injector(st))
    # The processor's own push_frame needs a linked pipeline; the reply text is
    # what this test is about, so collect it instead.
    rf._pushed = []

    async def _collect(frame, direction=FrameDirection.DOWNSTREAM):
        rf._pushed.append(frame)

    rf.push_frame = _collect
    return rf, st


async def _say(rf, text: str) -> None:
    """One complete agent reply, start to end, through the real frame path."""
    await rf.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    for chunk in (text, "\n"):
        await rf.process_frame(LLMTextFrame(chunk), FrameDirection.DOWNSTREAM)
    await rf.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)


@pytest.mark.asyncio
async def test_a_real_question_spends_an_ask():
    """The control. Without this the test below proves nothing."""
    rf, st = _filter()
    st.pending_ask = "location"
    await _say(rf, "మీరు ఏ ఏరియాలో ఉంటున్నారు?")
    assert st.ask_counts.get("location") == 1


@pytest.mark.asyncio
async def test_the_repair_line_spends_nothing():
    rf, st = _filter()
    st.pending_ask = "location"
    await _say(rf, guardrails.REPAIR_LINE.strip())

    assert st.ask_counts.get("location", 0) == 0, (
        "run 783: the caller was charged a question for an apology he was "
        "given because WE talked over him"
    )
    assert "location" in st.still_need


@pytest.mark.asyncio
async def test_two_repair_lines_still_leave_the_field_askable():
    """The failure mode in full: two apologies used to abandon the field."""
    rf, st = _filter()
    for _ in range(2):
        st.pending_ask = "location"
        await _say(rf, guardrails.REPAIR_LINE.strip())

    assert "location" not in st.abandoned
    assert "location" in st.still_need


@pytest.mark.asyncio
async def test_a_delivered_closing_is_recorded():
    """Run 803's counter must be driven by the pipeline, not only by tests.

    `closings_said` is what stops the seventh identical goodbye. It is
    incremented here, on the frame that means the reply was actually spoken --
    a barge-in flushes this frame, so an interrupted goodbye is correctly not
    counted.
    """
    rf, st = _filter()
    st.known = {"location": "హైదరాబాద్"}          # nothing left to ask -> closing
    assert st.closing_is_due()

    await _say(rf, "థాంక్యూ సార్, మంచి రోజు.")
    assert st.closings_said == 1
