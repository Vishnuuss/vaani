"""The LLM must not wait for the STT's final transcript.

Measured on run 3 (real Vobiz call, Sarvam saarika:v2.5): endpoint+STT averaged
1.332s of a 1.912s turn. The same agent on Deepgram averaged 0.7s. The gap is
not Sarvam being slow at recognition -- it is us holding the turn until
`transcript.final` arrives.

`bench/FINDINGS.md` measured 438ms p50 from flush to final on 2026-08-25 and
concluded: "respond off the partial, never wait for transcript.final."
Nothing was ever built. Verified by grep: InterimTranscriptionFrame reaches the
live path only in observers; LLMContextAggregator consumes it and never
forwards it.

PartialResponder closes that gap. It sits after STT and before the aggregator
(the only place interim frames exist) and, when the turn ends with no final in
hand, promotes the stable partial to a real TranscriptionFrame so the LLM can
start immediately.
"""

import pytest

from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from api.services.vaani.partial_response import PartialResponder


class _Sink:
    """Captures what the processor pushes downstream."""

    def __init__(self):
        self.frames = []

    async def __call__(self, frame, direction):
        self.frames.append(frame)


def _wire(responder, sink):
    responder.push_frame = sink
    return responder


def _texts(sink):
    return [f.text for f in sink.frames if isinstance(f, TranscriptionFrame)]


@pytest.mark.asyncio
async def test_turn_end_without_a_final_promotes_the_stable_partial():
    """The whole point: the LLM gets text at turn end instead of waiting."""
    sink = _Sink()
    r = _wire(PartialResponder(), sink)

    await r.process_frame(InterimTranscriptionFrame("five", "u", ""), FrameDirection.DOWNSTREAM)
    await r.process_frame(InterimTranscriptionFrame("five thousand", "u", ""), FrameDirection.DOWNSTREAM)
    await r.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert _texts(sink) == ["five thousand"], (
        "turn ended with no final transcript and no text reached the LLM -- "
        "this is the 0.6s"
    )


@pytest.mark.asyncio
async def test_a_real_final_wins_and_nothing_is_duplicated():
    """When the STT is fast enough, behave exactly as before."""
    sink = _Sink()
    r = _wire(PartialResponder(), sink)

    await r.process_frame(InterimTranscriptionFrame("five", "u", ""), FrameDirection.DOWNSTREAM)
    await r.process_frame(TranscriptionFrame("five thousand rupees", "u", ""), FrameDirection.DOWNSTREAM)
    await r.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert _texts(sink) == ["five thousand rupees"], "the final must not be duplicated"


@pytest.mark.asyncio
async def test_a_late_final_is_suppressed_after_we_already_spoke():
    """The late final would otherwise trigger a SECOND turn on the same speech."""
    sink = _Sink()
    r = _wire(PartialResponder(), sink)

    await r.process_frame(InterimTranscriptionFrame("five thousand", "u", ""), FrameDirection.DOWNSTREAM)
    await r.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await r.process_frame(TranscriptionFrame("five thousand", "u", ""), FrameDirection.DOWNSTREAM)

    assert _texts(sink) == ["five thousand"], "the late final must not re-fire the turn"


@pytest.mark.asyncio
async def test_silence_emits_nothing():
    """No speech must not manufacture an empty turn."""
    sink = _Sink()
    r = _wire(PartialResponder(), sink)
    await r.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert _texts(sink) == []


@pytest.mark.asyncio
async def test_the_next_turn_starts_clean():
    """State must reset, or turn 2 replays turn 1's words."""
    sink = _Sink()
    r = _wire(PartialResponder(), sink)

    await r.process_frame(InterimTranscriptionFrame("hyderabad", "u", ""), FrameDirection.DOWNSTREAM)
    await r.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await r.process_frame(InterimTranscriptionFrame("anantapur", "u", ""), FrameDirection.DOWNSTREAM)
    await r.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert _texts(sink) == ["hyderabad", "anantapur"]


@pytest.mark.asyncio
async def test_interim_frames_still_flow_downstream():
    """The dashboard and any observer must keep seeing partials."""
    sink = _Sink()
    r = _wire(PartialResponder(), sink)
    await r.process_frame(InterimTranscriptionFrame("five", "u", ""), FrameDirection.DOWNSTREAM)
    assert any(isinstance(f, InterimTranscriptionFrame) for f in sink.frames)
