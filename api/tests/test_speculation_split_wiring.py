"""Partials and the replay decision happen at different points in the pipeline.

`LLMContextAggregator` **consumes** InterimTranscriptionFrame and
TranscriptionFrame — llm_response_universal.py:797 — and does not push them
downstream. So a processor placed before the LLM (where the generation trigger
appears) never sees a partial, and one placed after STT never sees the trigger.

The work is therefore split, sharing one coordinator:

    after STT   SpeculationProbe  -> feeds partials, records the final text
    before LLM  SpeculativeLLMGate -> replays on a hit, swallows the trigger

These tests exist because the first wiring put both jobs in the gate, where the
partials never arrive and speculation silently did nothing.
"""

import pytest

from api.services.pipecat.speculation.coordinator import SpeculationCoordinator
from api.services.pipecat.speculation.gate import SpeculativeLLMGate
from api.services.pipecat.speculation.probe import SpeculationProbe
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    LLMRunFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection


class FakeLLM:
    def __init__(self, reply="సరే అండి"):
        self.reply = reply
        self.calls = []

    async def __call__(self, text):
        self.calls.append(text)
        for token in self.reply.split():
            yield token + " "


def _wire():
    llm = FakeLLM()
    coordinator = SpeculationCoordinator(generate=llm)
    probe = SpeculationProbe(coordinator=coordinator)
    gate = SpeculativeLLMGate(coordinator=coordinator)

    pushed = []

    async def _cap(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    probe.push_frame = _cap  # type: ignore[method-assign]
    gate.push_frame = _cap  # type: ignore[method-assign]
    return llm, probe, gate, pushed


async def _settle():
    import asyncio

    for _ in range(30):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_probe_feeds_partials_so_generation_starts_mid_turn():
    llm, probe, _gate, _ = _wire()

    for text in ("నా ఇల్లు", "నా ఇల్లు నాదే"):
        await probe.process_frame(
            InterimTranscriptionFrame(text, "user", ""), FrameDirection.DOWNSTREAM
        )
    await _settle()

    # The newest partial, not the lagging two-partial prefix. Superseded
    # generations are cancelled before their body runs, so only one appears.
    assert llm.calls == ["నా ఇల్లు నాదే"]


@pytest.mark.asyncio
async def test_the_gate_replays_using_the_final_text_the_probe_recorded():
    llm, probe, gate, pushed = _wire()

    for text in ("నా ఇల్లు", "నా ఇల్లు నాదే"):
        await probe.process_frame(
            InterimTranscriptionFrame(text, "user", ""), FrameDirection.DOWNSTREAM
        )
    await _settle()
    # The gate never sees this frame in the real pipeline — the probe does.
    await probe.process_frame(
        TranscriptionFrame("నా ఇల్లు నాదే", "user", ""), FrameDirection.DOWNSTREAM
    )

    await gate.process_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)

    spoken = "".join(f.text for f in pushed if isinstance(f, LLMTextFrame))
    assert spoken.strip() == "సరే అండి"
    assert not any(isinstance(f, LLMRunFrame) for f in pushed), "LLM must be skipped"


@pytest.mark.asyncio
async def test_a_mismatch_lets_the_real_llm_run():
    llm, probe, gate, pushed = _wire()

    for text in ("నాకు లోన్", "నాకు లోన్ కావాలి"):
        await probe.process_frame(
            InterimTranscriptionFrame(text, "user", ""), FrameDirection.DOWNSTREAM
        )
    await _settle()
    await probe.process_frame(
        TranscriptionFrame("నాకు ఇల్లు కావాలి", "user", ""), FrameDirection.DOWNSTREAM
    )

    trigger = LLMRunFrame()
    await gate.process_frame(trigger, FrameDirection.DOWNSTREAM)

    assert trigger in pushed, "on a miss the trigger must reach the LLM"


@pytest.mark.asyncio
async def test_every_turn_reports_its_outcome():
    """Speculation was enabled once before with no way to see whether it fired.

    That is how a 0% hit rate went unexplained -- and the cause turned out to be
    a trigger bug, not the traffic. It does not go on blind again.
    """
    recorded = []

    async def report(message):
        recorded.append(message)

    llm, probe, gate, _ = _wire()
    gate._report = report

    for text in ("నా ఇల్లు", "నా ఇల్లు నాదే"):
        await probe.process_frame(
            InterimTranscriptionFrame(text, "user", ""), FrameDirection.DOWNSTREAM
        )
    await _settle()
    await probe.process_frame(
        TranscriptionFrame("నా ఇల్లు నాదే", "user", ""), FrameDirection.DOWNSTREAM
    )
    await gate.process_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)

    assert recorded, "the turn produced no speculation record"
    assert recorded[0]["type"] == "rtf-speculation"
    assert recorded[0]["payload"]["outcome"] == "hit"


@pytest.mark.asyncio
async def test_a_miss_is_reported_too():
    """A miss is the number that matters most -- it is what says the trigger is
    wrong, which is exactly what was invisible last time."""
    recorded = []

    async def report(message):
        recorded.append(message)

    llm, probe, gate, _ = _wire()
    gate._report = report

    await probe.process_frame(
        InterimTranscriptionFrame("నా ఇల్లు", "user", ""), FrameDirection.DOWNSTREAM
    )
    await _settle()
    await probe.process_frame(
        TranscriptionFrame("వేరే మాట", "user", ""), FrameDirection.DOWNSTREAM
    )
    await gate.process_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)

    assert recorded and recorded[0]["payload"]["outcome"] == "miss"
