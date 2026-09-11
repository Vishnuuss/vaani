"""Run 889 (11 Sep): he said one thing in two breaths and was answered twice.

    18:09:52.966  USER  ఆ సరే. మాట్లాడవచ్చు.
    18:09:54.730  USER  సార్ అండి.                  second final, 1.8s later
    18:09:56.984  BOT   [reply to the first]
    18:09:58.544  BOT   [reply to the second]

Four times in a sixty-second call, and he said exactly what it sounded like
from his end:

    ఇన్ని సార్లు ఎవరైనా అడుగుతారా?       does anyone ask this many times?
    ఒకే క్వశ్చన్ రెండు క్వశ్చన్లు.        one question -- two questions
    సరే ఒక్క క్వశ్చన్ ఒకసారి అడగండి       ask one question at a time
    వన్ క్వశ్చన్ తర్వాత నెక్స్ట్ క్వశ్చన్  one question, then the next

Both finals landed before any audio was produced. Each became a turn and each
earned its own reply. `_stale` cannot catch this: neither reply is stale --
both were generated after their own turn ended, and neither was overtaken.

So the turn guard now holds a turn that arrives while a reply is being built
and none of it has been heard yet. The held text joins the next turn, and he
gets one reply covering everything he said.

The window is narrow on purpose. Once audio is out, `unheard` is False and the
hold does not apply: a caller talking over the agent is barge-in, and holding
his turn then would break it.
"""

import asyncio

from pipecat.turns.user_stop.base_user_turn_stop_strategy import (
    UserTurnStoppedParams,
)

from api.services.vaani.filler_turns import (
    FillerAwareUserTurnStopStrategy,
    ReplyInFlight,
)


class _Recorder:
    def __init__(self):
        self.stops = []

    async def on_stop(self, *_a, **_k):
        self.stops.append(1)


def _wrapped(in_flight, defer_secs=5.0):
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy

    w = FillerAwareUserTurnStopStrategy(
        SpeechTimeoutUserTurnStopStrategy(),
        defer_secs=defer_secs, max_defers=2, in_flight=in_flight)
    rec = _Recorder()
    w.add_event_handler("on_user_turn_stopped", rec.on_stop)
    return w, rec


async def _stop(w, text):
    w._text = text
    await w._on_inner_stopped(None, UserTurnStoppedParams(
        enable_user_speaking_frames=True))


# --- the flag itself -------------------------------------------------------

def test_unheard_is_only_true_between_the_first_token_and_the_first_word():
    f = ReplyInFlight()
    assert not f.unheard                 # nothing being built
    f.begin()
    assert f.unheard                     # building, nothing spoken
    f.note_spoken()
    assert not f.unheard                 # he can hear it: barge-in territory
    f.done()
    assert not f.unheard


# --- run 889 ---------------------------------------------------------------

def test_a_second_breath_is_held_while_the_reply_is_still_unheard():
    async def go():
        f = ReplyInFlight()
        w, rec = _wrapped(f)
        f.begin()                                   # reply to breath one
        await _stop(w, "సార్ అండి.")                 # breath two arrives
        assert rec.stops == [], (
            "the second breath earned its own reply -- run 889 exactly")
        w._cancel_timer()

    asyncio.run(go())


def test_once_he_can_hear_the_reply_his_turn_goes_straight_through():
    """Barge-in must not be held. A complete sentence is not a filler."""
    async def go():
        f = ReplyInFlight()
        w, rec = _wrapped(f)
        f.begin()
        f.note_spoken()
        await _stop(w, "ఆగండి నేను చెప్తాను")
        assert len(rec.stops) == 1, "barge-in was held; that is dead air"

    asyncio.run(go())


def test_with_no_reply_in_flight_nothing_changes():
    async def go():
        f = ReplyInFlight()
        w, rec = _wrapped(f)
        await _stop(w, "హైదరాబాద్")
        assert len(rec.stops) == 1

    asyncio.run(go())


def test_the_hold_is_still_bounded():
    """Two breaths may be merged. A caller cannot be held for ever."""
    async def go():
        f = ReplyInFlight()
        w, rec = _wrapped(f)
        f.begin()
        for _ in range(2):
            await _stop(w, "సార్ అండి.")
            assert rec.stops == []
        await _stop(w, "సార్ అండి.")
        assert len(rec.stops) == 1, "held past max_defers"
        w._cancel_timer()

    asyncio.run(go())


def test_a_guard_built_without_the_flag_behaves_exactly_as_before():
    async def go():
        w, rec = _wrapped(None)
        await _stop(w, "హైదరాబాద్")
        assert len(rec.stops) == 1

    asyncio.run(go())
