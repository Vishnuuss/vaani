"""Runs 885 and 887: six and eight seconds of dead air, and nothing recorded why.

    run 887 turn 10   endpoint 6.406s   (STT 0.270s -- the transcript was fast)
    run 885 turn  3   endpoint 8.055s   (STT 0.336s -- likewise)

The arithmetic points at one mechanism. The filler guard holds a hesitation for
1.2s, waiting for the rest of the sentence. If a bare VAD event arrives during
that hold, `_observe` CANCELS the release timer and hands the decision back to
the inner strategy -- which is pipecat's speech timeout, constructed with
`wait_for_transcript=True`. Noise produces no transcript, so the inner strategy
never fires again and the turn is stranded until the 5.0s backstop:

    1.2 (hold) + 5.0 (backstop)             = 6.2s   vs 6.406s measured
    1.2 + 1.2 (two holds) + 5.0 + 0.8 floor = 8.2s   vs 8.055s measured

This codebase already learned the same lesson on the reply side, today:
"A reply is abandoned only on EVIDENCE that he really spoke ... bare VAD fires
without the caller saying anything new." Run 881 is the extreme case -- the
agent's own greeting came back as caller audio off a speakerphone.

So the release timer is cancelled by new TEXT, which is evidence he spoke, and
not by a VAD edge, which is not. Deferring must never become discarding, and a
watchdog that a stray noise can switch off is not a watchdog.
"""

import asyncio

from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    UserStartedSpeakingFrame,
)
from pipecat.turns.user_stop.base_user_turn_stop_strategy import (
    UserTurnStoppedParams,
)

from api.services.vaani.filler_turns import FillerAwareUserTurnStopStrategy


class _Recorder:
    def __init__(self):
        self.stops = []

    async def on_stop(self, *_a, **_k):
        self.stops.append(asyncio.get_running_loop().time())


def _wrapped(defer_secs=0.05, max_defers=2):
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy

    w = FillerAwareUserTurnStopStrategy(
        SpeechTimeoutUserTurnStopStrategy(),
        defer_secs=defer_secs, max_defers=max_defers)
    rec = _Recorder()
    w.add_event_handler("on_user_turn_stopped", rec.on_stop)
    return w, rec


def test_a_bare_vad_edge_does_not_strand_the_held_turn():
    """Run 887 turn 10. Noise during the hold must not cancel the watchdog."""
    async def go():
        w, rec = _wrapped(defer_secs=0.05)
        w._text = "ఆ"
        await w._on_inner_stopped(None, UserTurnStoppedParams(
            enable_user_speaking_frames=True))
        assert rec.stops == [], "the filler was not held at all"

        w._observe(UserStartedSpeakingFrame())      # VAD only: no transcript

        await asyncio.sleep(0.15)
        assert len(rec.stops) == 1, (
            "a bare VAD edge cancelled the watchdog and stranded the turn")

    asyncio.run(go())


def test_real_speech_still_cancels_the_hold():
    """Evidence he actually spoke: the inner strategy decides afresh."""
    async def go():
        w, rec = _wrapped(defer_secs=0.05)
        w._text = "ఆ"
        await w._on_inner_stopped(None, UserTurnStoppedParams(
            enable_user_speaking_frames=True))
        assert rec.stops == []

        w._observe(InterimTranscriptionFrame(
            user_id="", text="హైదరాబాద్", timestamp=""))

        await asyncio.sleep(0.15)
        assert rec.stops == [], (
            "new speech arrived, so the held turn should have been withdrawn")
        assert "హైదరాబాద్" in w._text

    asyncio.run(go())


def test_the_watchdog_still_fires_when_nothing_follows():
    """The load-bearing guarantee, unchanged: deferring is never discarding."""
    async def go():
        w, rec = _wrapped(defer_secs=0.05)
        w._text = "ఆ"
        await w._on_inner_stopped(None, UserTurnStoppedParams(
            enable_user_speaking_frames=True))
        await asyncio.sleep(0.15)
        assert len(rec.stops) == 1

    asyncio.run(go())
