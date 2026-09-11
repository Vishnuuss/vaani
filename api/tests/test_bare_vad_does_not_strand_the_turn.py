"""Runs 885, 887 and 888: the dead air, and the fragmentation caused by fixing it wrong.

Run 887 turn 10 measured a 6.406s endpoint with the transcript arriving in
0.270s; run 885 turn 3 measured 8.055s. The arithmetic names the mechanism. The
filler guard holds a hesitation for 1.2s waiting for the rest of the sentence,
and a VAD edge during that hold cancelled the release timer outright -- handing
the turn back to pipecat's speech timeout, which is built with
`wait_for_transcript=True`. Noise carries no transcript, so the inner strategy
never fired again and the turn sat stranded until the 5.0s backstop:

    1.2 + 5.0             = 6.2s   vs 6.406s measured
    1.2 + 1.2 + 5.0 + 0.8 = 8.2s   vs 8.055s measured

The FIRST attempt at this moved the cancellation onto the transcript instead of
the VAD edge, so that only evidence of speech could withdraw a hold. Run 888
shows why that was wrong. Text arrives AFTER the words are finished, so
withdrawing on text released the turn the moment a fragment landed, and one
sentence became two turns with a reply each -- two bot utterances on nearly
every exchange, until the caller asked "ఎన్ని సార్లు అడుగుతారండి దీన్ని?" (how
many times are you going to ask this?) and hung up with no field filled.

So the VAD edge withdraws the hold, as it always did -- it is the only signal
that arrives BEFORE the words. What changed is that withdrawing now leaves a
last-resort watchdog behind instead of nothing. The inner strategy is still
free to end the turn first and normally does. A turn that was held once can no
longer be stranded by a noise.
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


def _wrapped(defer_secs=0.05, max_defers=2, backstop_secs=0.05):
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy

    w = FillerAwareUserTurnStopStrategy(
        SpeechTimeoutUserTurnStopStrategy(),
        defer_secs=defer_secs, max_defers=max_defers,
        backstop_secs=backstop_secs)
    rec = _Recorder()
    w.add_event_handler("on_user_turn_stopped", rec.on_stop)
    return w, rec


async def _hold(w):
    w._text = "ఆ"
    await w._on_inner_stopped(None, UserTurnStoppedParams(
        enable_user_speaking_frames=True))


def test_a_bare_vad_edge_can_no_longer_strand_the_held_turn():
    """Runs 885 and 887. Withdrawing the hold must leave a watchdog behind."""
    async def go():
        w, rec = _wrapped()
        await _hold(w)
        assert rec.stops == [], "the filler was not held at all"

        w._observe(UserStartedSpeakingFrame())      # VAD only: no transcript

        await asyncio.sleep(0.2)
        assert len(rec.stops) == 1, (
            "a bare VAD edge stranded the turn -- nothing was left to release it")

    asyncio.run(go())


def test_the_vad_edge_still_withdraws_the_hold_so_the_sentence_stays_whole():
    """Run 888. Text arrives after the words; only the VAD edge arrives before.

    Withdrawing on a fragment of text is what tore one sentence into two turns.
    """
    async def go():
        w, rec = _wrapped(defer_secs=5.0, backstop_secs=5.0)
        await _hold(w)

        w._observe(InterimTranscriptionFrame(
            user_id="", text="హైదరాబాద్", timestamp=""))

        await asyncio.sleep(0.2)
        assert rec.stops == [], (
            "a fragment of text released the turn; run 888's two replies per "
            "exchange come from exactly this")
        assert "హైదరాబాద్" in w._text, "the fragment must still accumulate"
        w._cancel_timer()

    asyncio.run(go())


def test_the_watchdog_still_fires_when_nothing_follows():
    """The load-bearing guarantee, unchanged: deferring is never discarding."""
    async def go():
        w, rec = _wrapped()
        await _hold(w)
        await asyncio.sleep(0.2)
        assert len(rec.stops) == 1

    asyncio.run(go())


# --- runs 893 and 894: the watchdog must not be postponable ----------------

def test_repeated_vad_edges_cannot_push_the_turn_out_for_ever():
    """A caller saying "hello ... hello ... hello" re-armed it every time.

        18:54:22.662  BOT   మీది సొంత ఇల్లా...?
                [19.5s of nothing]
        18:54:42.173  USER  కమర్షియల్ ఏ. హలో. హలో. హలో. హలో.

    Both calls stalled on his first real answer and both ended with him hanging
    up. The wait now runs to an absolute deadline fixed when the hold began, so
    re-arming can only ever shorten it.
    """
    async def go():
        w, rec = _wrapped(defer_secs=0.05, max_defers=2, backstop_secs=0.05)
        await _hold(w)
        deadline = w._deadline
        assert deadline is not None, "no deadline was fixed when the hold began"

        for _ in range(20):                      # he keeps saying hello
            w._observe(UserStartedSpeakingFrame())
            # Once the turn is out the deadline is cleared, which is correct.
            # What must never happen is it moving further away.
            assert w._deadline in (deadline, None), "the deadline moved"
            await asyncio.sleep(0.01)

        await asyncio.sleep(0.3)
        assert len(rec.stops) == 1, (
            "repeated VAD edges postponed the turn indefinitely -- runs 893/894")

    asyncio.run(go())
