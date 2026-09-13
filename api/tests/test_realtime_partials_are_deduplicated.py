"""A re-scored partial that says nothing new is not pushed.

Measured with `tools/probe_sarvam_realtime.py` on a real 38.89s caller
recording, 8 kHz Telugu: **127 partials for 6 finals.** The decoder re-scores
continuously and re-sends text it has already sent:

    "నో" / "నో" / "నో నో" / "నో నో" / "నో నో నో" / ""

Roughly half carry no new words. Each is still a frame pushed onto the critical
path, mid-sentence, past every downstream observer.

Run 969 is the cost. Switching production to this service moved the endpoint leg
from 0.888s to 1.789s, spread 1.720-1.887 across ten turns. That flatness is the
finding: a caller-driven number varies, a fallback firing every turn does not.
The STT is NOT the slow part -- the same probe measures speech_end -> final at
p50 0.115s against saarika:v2.5's 0.373s.

Empty partials were already dropped, for a different reason: they would clear
text the aggregator is holding. This drops the unchanged ones.
"""

from __future__ import annotations

import pytest

from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
)

from api.services.vaani.sarvam_realtime_stt import SarvamRealtimeSTTService


class _Svc(SarvamRealtimeSTTService):
    """The service with the socket removed and pushes recorded."""

    def __init__(self):
        super().__init__(api_key="x", language="te-IN", sample_rate=8000)
        self.pushed = []

    async def push_frame(self, frame, direction=None):
        self.pushed.append(frame)

    async def push_error(self, error_msg=""):
        self.pushed.append(("error", error_msg))


def _interims(svc):
    return [f.text for f in svc.pushed
            if isinstance(f, InterimTranscriptionFrame)]


def _finals(svc):
    return [f.text for f in svc.pushed if isinstance(f, TranscriptionFrame)]


@pytest.mark.asyncio
async def test_the_rescore_flood_is_collapsed():
    """The exact sequence the probe recorded, minus the empties."""
    svc = _Svc()
    for t in ["నో", "నో", "నో నో", "నో నో", "నో నో నో", "నో నో నో"]:
        await svc._handle({"event": "transcript.partial", "text": t})
    assert _interims(svc) == ["నో", "నో నో", "నో నో నో"], (
        "a re-score that says nothing new was pushed anyway")


@pytest.mark.asyncio
async def test_growth_is_never_suppressed():
    """Only repeats are dropped. Every new word still reaches the pipeline."""
    svc = _Svc()
    for t in ["నా", "నా పేరు", "నా పేరు రమేష్"]:
        await svc._handle({"event": "transcript.partial", "text": t})
    assert _interims(svc) == ["నా", "నా పేరు", "నా పేరు రమేష్"]


@pytest.mark.asyncio
async def test_the_next_utterance_starts_clean():
    """A repeat across a final is a NEW utterance, not a re-score.

    Without the reset, a caller who says the same short word twice in a row --
    "ఆ" then, after a reply, "ఆ" again -- would have the second one silently
    dropped.
    """
    svc = _Svc()
    await svc._handle({"event": "transcript.partial", "text": "ఆ"})
    await svc._handle({"event": "transcript.final", "text": "ఆ"})
    await svc._handle({"event": "transcript.partial", "text": "ఆ"})
    assert _interims(svc) == ["ఆ", "ఆ"], "the new utterance's partial was eaten"


@pytest.mark.asyncio
async def test_empty_partials_are_still_dropped():
    """Unchanged behaviour: an empty would clear held text."""
    svc = _Svc()
    await svc._handle({"event": "transcript.partial", "text": "నో"})
    await svc._handle({"event": "transcript.partial", "text": ""})
    await svc._handle({"event": "transcript.partial", "text": "నో నో"})
    assert _interims(svc) == ["నో", "నో నో"]


@pytest.mark.asyncio
async def test_finals_are_untouched_by_this():
    """The one-turn-per-utterance guard must not be affected."""
    svc = _Svc()
    await svc._handle({"event": "transcript.final", "text": "నా పేరు"})
    await svc._handle({"event": "transcript.final", "text": "నా పేరు రమేష్"})
    assert _finals(svc) == ["నా పేరు", "రమేష్"], (
        "the delta emission changed; the aggregator would now stutter")
