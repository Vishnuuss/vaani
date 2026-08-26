"""The gate that takes the LLM off the critical path.

Sits immediately before the LLM service. On a speculation hit it emits the
already-generated tokens as a normal LLM response and **swallows the trigger
frame**, so the real LLM never runs and its time-to-first-byte (measured p50
1.35 s on the current provider) disappears from the turn.

On a miss it forwards the trigger frame untouched and the pipeline behaves
exactly as it does today.
"""

import pytest

from api.services.pipecat.speculation.gate import SpeculativeLLMGate
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection


class FakeCoordinator:
    """Stands in for SpeculationCoordinator with a scripted verdict."""

    def __init__(self, response=None):
        self._response = response
        self.partials: list[str] = []
        self.asked_for: list[str] = []

    async def on_partial(self, text):
        self.partials.append(text)

    async def take_response_for(self, text):
        self.asked_for.append(text)
        return self._response

    def reset_turn(self):
        pass


def _gate(coordinator):
    gate = SpeculativeLLMGate(coordinator=coordinator)
    pushed = []

    async def _capture(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    gate.push_frame = _capture  # type: ignore[method-assign]
    return gate, pushed


@pytest.mark.asyncio
async def test_partials_are_forwarded_to_the_coordinator():
    coord = FakeCoordinator()
    gate, _ = _gate(coord)

    await gate.process_frame(
        InterimTranscriptionFrame("నా ఇల్లు", "user", ""), FrameDirection.DOWNSTREAM
    )

    assert coord.partials == ["నా ఇల్లు"]


@pytest.mark.asyncio
async def test_on_a_hit_it_emits_the_pre_generated_response_and_swallows_the_trigger():
    coord = FakeCoordinator(response=["అలాగే ", "అండి"])
    gate, pushed = _gate(coord)

    await gate.process_frame(
        InterimTranscriptionFrame("అవును", "user", ""), FrameDirection.DOWNSTREAM
    )
    await gate.process_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)

    kinds = [type(f) for f in pushed]
    assert LLMRunFrame not in kinds, "the real LLM must not be triggered on a hit"
    assert kinds.count(LLMFullResponseStartFrame) == 1
    assert kinds.count(LLMFullResponseEndFrame) == 1
    text = "".join(f.text for f in pushed if isinstance(f, LLMTextFrame))
    assert text == "అలాగే అండి"


@pytest.mark.asyncio
async def test_on_a_miss_the_trigger_frame_passes_through_untouched():
    coord = FakeCoordinator(response=None)
    gate, pushed = _gate(coord)

    trigger = LLMRunFrame()
    await gate.process_frame(trigger, FrameDirection.DOWNSTREAM)

    assert pushed == [trigger]


@pytest.mark.asyncio
async def test_a_coordinator_failure_never_blocks_the_turn():
    class Broken(FakeCoordinator):
        async def take_response_for(self, text):
            raise RuntimeError("boom")

    gate, pushed = _gate(Broken())
    trigger = LLMRunFrame()

    await gate.process_frame(trigger, FrameDirection.DOWNSTREAM)

    # Falls back to the normal path rather than dropping the caller's turn.
    assert pushed == [trigger]
