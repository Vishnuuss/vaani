"""One turn per utterance, not one per stabilised segment.

Run 780 was the first live call after `saaras:v3-realtime` was enabled, and its
user transcript is the word "హలో" repeated TWENTY-TWO times, with fragments "హ"
among them. The caller said it once.

Sarvam's realtime endpoint sends a `final` per stabilised SEGMENT and re-sends
one whenever the decoder re-scores. Each was pushed as a `TranscriptionFrame`
with `finalized=True`, which downstream is a whole TURN. The LLM was then
re-reading a context full of duplicates: 3.668s on one turn, against 0.351s on
the previous call with the old model. Reverted within the hour.

The service's own header already stated the rule it was breaking: "This service
reports words. It does not decide turns."
"""

from __future__ import annotations

import pytest

from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStoppedSpeakingFrame,
)

from api.services.vaani.sarvam_realtime_stt import SarvamRealtimeSTTService


class _Recorder:
    def __init__(self):
        self.frames = []

    async def __call__(self, frame, *a, **kw):
        self.frames.append(frame)


def _service() -> SarvamRealtimeSTTService:
    svc = SarvamRealtimeSTTService(api_key="test", language="te-IN")
    svc.push_frame = _Recorder()
    return svc


def _finals(svc) -> list[str]:
    return [f.text for f in svc.push_frame.frames
            if isinstance(f, TranscriptionFrame)]


def _interims(svc) -> list[str]:
    return [f.text for f in svc.push_frame.frames
            if isinstance(f, InterimTranscriptionFrame)]


@pytest.mark.asyncio
async def test_a_rescored_final_does_not_become_a_second_turn():
    """The run 780 case, minimised."""
    svc = _service()
    for _ in range(22):
        await svc._handle({"event": "final", "text": "హలో"})
    assert _finals(svc) == ["హలో"], "one utterance must produce one turn"


@pytest.mark.asyncio
async def test_a_fragment_of_what_was_already_said_is_ignored():
    """"హ" after "హలో" is the decoder going backwards, not the caller."""
    svc = _service()
    await svc._handle({"event": "final", "text": "హలో"})
    await svc._handle({"event": "final", "text": "హ"})
    assert _finals(svc) == ["హలో"]


@pytest.mark.asyncio
async def test_a_growing_utterance_is_emitted():
    """The aggregator must end up holding the WHOLE utterance, not its head."""
    svc = _service()
    await svc._handle({"event": "final", "text": "నా పేరు"})
    await svc._handle({"event": "final", "text": "నా పేరు రమేష్"})
    assert _finals(svc) == ["నా పేరు", "నా పేరు రమేష్"]


@pytest.mark.asyncio
async def test_the_same_word_in_a_LATER_turn_is_not_swallowed():
    """"సరే" answered to two different questions is two answers. The guard must
    not leak across the turn boundary."""
    svc = _service()
    await svc._handle({"event": "final", "text": "సరే"})
    await svc.process_frame(UserStoppedSpeakingFrame(), None)
    await svc._handle({"event": "final", "text": "సరే"})
    assert _finals(svc) == ["సరే", "సరే"]


@pytest.mark.asyncio
async def test_partials_are_still_interim_and_are_not_deduped():
    """Partials are not turns, so they are not the thing being guarded."""
    svc = _service()
    await svc._handle({"event": "partial", "text": "హ"})
    await svc._handle({"event": "partial", "text": "హలో"})
    assert _interims(svc) == ["హ", "హలో"]
    assert _finals(svc) == []


@pytest.mark.asyncio
async def test_an_empty_message_changes_nothing():
    svc = _service()
    await svc._handle({"event": "final", "text": "   "})
    assert _finals(svc) == []
