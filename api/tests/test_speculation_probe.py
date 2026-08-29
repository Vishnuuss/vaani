"""The probe that measures the speculation hit rate on real calls.

Deliberately a PASS-THROUGH observer: it must never alter, drop or delay a
frame. Its only job is to turn the live partial stream into the hit-rate number
the <700 ms case depends on, so that the expensive change (actually reusing a
pre-generated response) is decided on evidence rather than hope.
"""

import pytest

from api.services.pipecat.speculation.probe import SpeculationProbe
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection


class _Sink:
    """Captures whatever the probe pushes downstream."""

    def __init__(self):
        self.frames = []


def _make_probe():
    probe = SpeculationProbe()
    sink = _Sink()

    async def _capture(frame, direction=FrameDirection.DOWNSTREAM):
        sink.frames.append(frame)

    probe.push_frame = _capture  # type: ignore[method-assign]
    return probe, sink


def _interim(text):
    return InterimTranscriptionFrame(text, "user", "")


def _final(text):
    return TranscriptionFrame(text, "user", "")


@pytest.mark.asyncio
async def test_it_passes_every_frame_through_untouched():
    probe, sink = _make_probe()

    frames = [_interim("నా"), _interim("నా ఇల్లు"), _final("నా ఇల్లు")]
    for frame in frames:
        await probe.process_frame(frame, FrameDirection.DOWNSTREAM)

    assert sink.frames == frames


@pytest.mark.asyncio
async def test_it_scores_a_clean_hit_from_the_live_frame_stream():
    probe, _ = _make_probe()

    for frame in (_interim("నా ఇల్లు"), _interim("నా ఇల్లు నాదే")):
        await probe.process_frame(frame, FrameDirection.DOWNSTREAM)
    await probe.process_frame(_final("నా ఇల్లు నాదే"), FrameDirection.DOWNSTREAM)

    assert probe.stats.turns == 1
    assert probe.stats.hits == 1


@pytest.mark.asyncio
async def test_each_final_transcript_starts_a_fresh_turn():
    probe, _ = _make_probe()

    for frame in (_interim("అవును"), _interim("అవును సరే"), _final("అవును సరే")):
        await probe.process_frame(frame, FrameDirection.DOWNSTREAM)
    for frame in (_interim("కాదు"), _interim("కాదు లేదు"), _final("వేరే మాట")):
        await probe.process_frame(frame, FrameDirection.DOWNSTREAM)

    assert probe.stats.turns == 2
    assert probe.stats.hits == 1
    assert probe.stats.misses == 1
