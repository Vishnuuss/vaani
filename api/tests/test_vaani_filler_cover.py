"""The cover must run INTO the reply, not stop short of it.

Run 267 is the failure this replaces. The first version played one clip at the
VAD stop and stopped; the reply arrived 1.0-1.8s later:

    36.80s  filler 0.17s          113.52s  filler 0.37s
    37.80s  the sentence          115.34s  the sentence

A word, then a second of silence, then the answer -- which the client heard
immediately and called "that aaa in the middle". Worse than plain silence.

Two properties decide whether this version is an improvement, and they pull
against each other:

  - it must keep speaking for as long as the gap lasts, and
  - it must get out of the way the instant the real reply has audio, without
    the reply queueing up behind it.

The second is the one that can do harm: a cover that delays the answer is the
defect, not the fix.
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from api.services.vaani import fillers as bank
from api.services.vaani.filler_player import (
    GAP_MS,
    MAX_COVER_S,
    FillerPlayer,
    FillerState,
)
from pipecat.frames.frames import (
    OutputAudioRawFrame,
    TTSAudioRawFrame,
    UserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

SR = 8000


class Analyzer:
    def __init__(self, p): self._last_probability = p


def build(monkeypatch, *, p=0.99, **kw):
    pcm = (np.zeros(int(0.25 * SR), dtype=np.int16) + 500).tobytes()
    monkeypatch.setattr(bank, "load_cached", lambda *a, **k: pcm)
    fp = FillerPlayer(state=FillerState(), turn_analyzer=Analyzer(p),
                      sample_rate=SR, **kw)
    pushed: list = []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((time.monotonic(), frame))

    monkeypatch.setattr(fp, "push_frame", capture)
    return fp, pushed


def audio_seconds(pushed) -> float:
    n = sum(len(f.audio) for _, f in pushed if isinstance(f, OutputAudioRawFrame))
    return n / 2 / SR


def longest_silence(pushed) -> float:
    """The longest stretch the caller hears nothing.

    The user-facing property. Total audio volume is the wrong measure: the cover
    breathes between words on purpose, so it is 60% speech by design. What made
    run 267 unacceptable was a SINGLE 1.0-1.8s hole, not the ratio.
    """
    times = [t for t, f in pushed if isinstance(f, OutputAudioRawFrame)]
    if len(times) < 2:
        return float("inf")
    return max(b - a for a, b in zip(times, times[1:]))


async def stop(fp):
    fp._armed = True
    await fp.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)


# --- it covers the whole gap -------------------------------------------------


@pytest.mark.asyncio
async def test_the_caller_never_hears_a_hole(monkeypatch):
    """Run 267's defect was a 1.0-1.8s hole between the filler and the reply.

    The cover breathes between words deliberately, so it is not continuous
    audio. What must never happen again is a stretch long enough to read as the
    agent having stopped.
    """
    fp, pushed = build(monkeypatch)
    await stop(fp)
    await asyncio.sleep(1.2)
    await fp._stop_cover()
    gap = longest_silence(pushed)
    assert gap < 0.40, f"the caller heard {gap:.2f}s of silence mid-cover"
    assert audio_seconds(pushed) > 0.5, "the cover stopped early"


@pytest.mark.asyncio
async def test_it_starts_almost_immediately(monkeypatch):
    """The point is speech at ~0.2s, not at 1.0s."""
    fp, pushed = build(monkeypatch)
    await stop(fp)
    await asyncio.sleep(0.1)
    await fp._stop_cover()
    assert audio_seconds(pushed) > 0, "nothing was spoken in the first 100ms"


# --- it gets out of the way --------------------------------------------------


@pytest.mark.asyncio
async def test_the_real_reply_stops_the_cover_at_once(monkeypatch):
    """The seam. A cover that outlives the reply is the old defect returning."""
    fp, pushed = build(monkeypatch)
    await stop(fp)
    await asyncio.sleep(0.2)
    await fp.process_frame(
        TTSAudioRawFrame(audio=b"\x00\x00", sample_rate=SR, num_channels=1),
        FrameDirection.DOWNSTREAM)
    assert fp._task is None, "the cover task must be cancelled by real audio"
    before = audio_seconds(pushed)
    await asyncio.sleep(0.2)
    assert audio_seconds(pushed) == pytest.approx(before, abs=1e-9), (
        "the cover kept speaking over the reply")


@pytest.mark.asyncio
async def test_the_reply_is_never_queued_behind_a_whole_clip(monkeypatch):
    """Pacing is the design.

    Emitting a clip in one go would put the reply behind all of it. At most one
    chunk may be outstanding when the reply arrives.
    """
    fp, pushed = build(monkeypatch)
    await stop(fp)
    await asyncio.sleep(0.05)
    spoken = audio_seconds(pushed)
    await fp._stop_cover()
    assert spoken < 0.12, (
        f"{spoken:.3f}s already queued after 50ms -- the reply would wait for it")


@pytest.mark.asyncio
async def test_the_caller_speaking_again_stops_it(monkeypatch):
    fp, pushed = build(monkeypatch)
    await stop(fp)
    await asyncio.sleep(0.1)
    await fp.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert fp._task is None
    before = audio_seconds(pushed)
    await asyncio.sleep(0.15)
    assert audio_seconds(pushed) == pytest.approx(before, abs=1e-9)


# --- it cannot run away ------------------------------------------------------


@pytest.mark.asyncio
async def test_it_gives_up_rather_than_murmuring_forever(monkeypatch):
    """If the reply never comes, silence beats an agent that will not stop."""
    fp, pushed = build(monkeypatch)
    await stop(fp)
    await asyncio.sleep(MAX_COVER_S + 0.4)
    assert audio_seconds(pushed) <= MAX_COVER_S + 0.3
    await fp._stop_cover()


@pytest.mark.asyncio
async def test_one_pause_produces_one_cover(monkeypatch):
    """A second VAD stop inside the same turn must not stack a second cover."""
    fp, _ = build(monkeypatch)
    await stop(fp)
    first = fp._task
    fp._armed = True
    await fp.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert fp._task is first, "a second cover was started over the first"
    await fp._stop_cover()


@pytest.mark.asyncio
async def test_an_unconfident_detector_stays_silent(monkeypatch):
    """Speaking over a caller who paused for breath is the one real risk."""
    fp, pushed = build(monkeypatch, p=0.50)
    await stop(fp)
    await asyncio.sleep(0.15)
    assert fp._task is None
    assert audio_seconds(pushed) == 0


@pytest.mark.asyncio
async def test_no_clips_means_no_cover(monkeypatch):
    monkeypatch.setattr(bank, "load_cached", lambda *a, **k: None)
    fp = FillerPlayer(state=FillerState(), turn_analyzer=Analyzer(0.99))
    assert fp.active is False


def test_the_breath_between_clips_is_shorter_than_the_gap_it_hides():
    """A 220ms pause reads as thinking; a second of it is the original defect."""
    assert GAP_MS < 400
