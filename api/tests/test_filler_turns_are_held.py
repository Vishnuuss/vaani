"""A thinking noise must not become a turn, and must not become silence either.

Run 863, the call the client rang about
----------------------------------------
Five of twenty caller turns were a single hesitation syllable, and each became
a finished user turn that generated a full LLM reply:

    17:21:16  "ఆ"   -> reply generated
    17:21:19  "ఉ"   -> reply generated
    17:21:20  BOT   which property type?
    17:21:21  BOT   what is your monthly bill?

Two replies 1.3 s apart, to two noises, neither of which was an answer.

Why the guard has to live outside the analyzer
-----------------------------------------------
`completeness.sounds_unfinished()` already knew "ఆ" was not a sentence. It was
consulted only inside the `turn_analyzer` branch, so moving to
`turn_stop_strategy = "transcription"` -- which is what runs after the detector
comparison -- dropped the knowledge silently. These tests pin the guard to
BOTH paths.

The two failure directions
---------------------------
Holding too little brings run 863 back. Holding too much is worse: a caller
whose entire answer is "ఆ" -- which run 314 shows happens -- would be met with
silence. So every test here comes in a pair, and the watchdog is tested
explicitly.
"""

import asyncio

import pytest

from api.services.vaani.filler_turns import (
    FillerAwareUserTurnStopStrategy,
    apply_filler_guard,
    is_only_filler,
    should_hold,
)
from api.services.vaani.turn_taking import (
    analyzer_from,
    create_user_turn_stop_strategies,
)
from pipecat.turns.user_stop.base_user_turn_stop_strategy import (
    UserTurnStoppedParams,
)


# --- 1. what gets held ----------------------------------------------------

# Every one of these was a real caller turn in run 863.
HOLD = ["ఆ", "ఉ", "ఒక", "ఆ.", "ఉ.", "అరవై"]

RELEASE = [
    "చెప్పండి.",                                # an instruction, not a noise
    "సంతల్లే.",
    "మాది ఒక 70 టు 80 లాక్స్ అప్ల వస్తుందండి.",
    "70 టు 80 లాక్స్",
    "అవును అండి.",
    "హైదరాబాద్.",
    "నితేష్.",
    "ఆ చెప్పండి.",                              # "ఆ" leads, but he answered
    "ఉన్నాము ఉన్నాము.",                          # repetition is emphasis
    "ఓకే ఓకే.",
    "హలో.",
    "ఒక ప్యానెల్",
    "ఒక ఫ్యాన్ కాస్ట్ ఎంత?",
    "సొంతమే",
]


def test_the_filler_turns_from_run_863_are_held():
    missed = [t for t in HOLD if not should_hold(t)]
    assert not missed, f"these became replies to nothing in run 863: {missed}"


def test_every_real_answer_is_released():
    """The expensive direction. Holding an answer delays the whole call."""
    wrong = [t for t in RELEASE if should_hold(t)]
    assert not wrong, f"held a real answer: {wrong}"


def test_an_empty_transcript_is_not_held():
    """No text is the STT being late, which is a different condition.

    Holding on empty would delay every turn whose transcript has not landed
    yet -- which, on `saarika:v2.5`, is most of them.
    """
    assert not should_hold("")
    assert not should_hold("   ")


def test_is_only_filler_is_the_narrow_case():
    assert is_only_filler("ఆ")
    assert is_only_filler("ఆ ఆ")
    assert is_only_filler("um")
    # "ఒక" is a word, not a noise; it is held by grammar, not by the lexicon.
    assert not is_only_filler("ఒక")
    assert should_hold("ఒక")


# --- 2. it is wired to BOTH detectors ------------------------------------


def test_the_guard_wraps_whichever_detector_is_configured():
    for strategy in ("transcription", "turn_analyzer"):
        s = create_user_turn_stop_strategies(
            {"turn_stop_strategy": strategy, "filler_turn_guard_enabled": True},
            uses_external_turns=False)
        assert isinstance(s[-1], FillerAwareUserTurnStopStrategy), (
            f"{strategy} left unguarded -- this is exactly how the knowledge "
            "was lost when the detector changed")


def test_off_by_default_changes_nothing():
    """Disabled must return the SAME objects, not equivalent ones."""
    inner = create_user_turn_stop_strategies(
        {"turn_stop_strategy": "transcription"}, uses_external_turns=False)
    assert not isinstance(inner[-1], FillerAwareUserTurnStopStrategy)
    same = apply_filler_guard(inner, {})
    assert same is inner


def test_external_turns_are_never_wrapped():
    """Another system owns turn boundaries there."""
    s = create_user_turn_stop_strategies(
        {"filler_turn_guard_enabled": True}, uses_external_turns=True)
    assert not isinstance(s[-1], FillerAwareUserTurnStopStrategy)


def test_the_analyzer_still_resolves_through_the_wrapper():
    """A wrapper that hides the analyzer switches the fillers off, silently.

    That exact failure has happened here before, when semantic turn completion
    was enabled: `analyzer_from` walked past `DeferredUserTurnStopStrategy`,
    returned None, and `FillerPlayer` was built inert. It took a live call to
    notice and nothing logged.
    """
    s = create_user_turn_stop_strategies(
        {"turn_stop_strategy": "turn_analyzer", "filler_turn_guard_enabled": True},
        uses_external_turns=False)
    assert analyzer_from(s) is not None


# --- 3. deferring is never discarding ------------------------------------


class _Recorder:
    """Captures what the wrapper emits upstream, with timings."""

    def __init__(self):
        self.stops: list[float] = []

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


def test_a_real_answer_is_emitted_immediately():
    async def go():
        w, rec = _wrapped()
        w._text = "హైదరాబాద్"
        await w._on_inner_stopped(None, UserTurnStoppedParams(
            enable_user_speaking_frames=True))
        assert len(rec.stops) == 1, "a real answer was delayed"

    asyncio.run(go())


def test_a_filler_is_held_then_released_by_the_watchdog():
    """The load-bearing test. Deferring must never become discarding.

    Nothing here speaks again after the filler. If the turn does not come out
    on its own, a caller whose whole answer was "ఆ" gets silence until the
    idle timeout -- worse than the bug being fixed.
    """
    async def go():
        w, rec = _wrapped(defer_secs=0.05)
        w._text = "ఆ"
        await w._on_inner_stopped(None, UserTurnStoppedParams(
            enable_user_speaking_frames=True))
        assert rec.stops == [], "the filler was not held at all"
        await asyncio.sleep(0.15)
        assert len(rec.stops) == 1, "the filler turn was never released"

    asyncio.run(go())


def test_the_hold_is_bounded():
    """"ఆ ... ఆ ... ఆ" cannot defer for ever."""
    async def go():
        w, rec = _wrapped(defer_secs=5.0, max_defers=2)
        params = UserTurnStoppedParams(enable_user_speaking_frames=True)
        for _ in range(2):
            w._text = "ఆ"
            await w._on_inner_stopped(None, params)
            assert rec.stops == []
        # The third goes straight through rather than waiting 5s again.
        w._text = "ఆ"
        await w._on_inner_stopped(None, params)
        assert len(rec.stops) == 1, "held past max_defers"
        w._cancel_timer()

    asyncio.run(go())


def test_zero_defer_disables_the_hold():
    async def go():
        w, rec = _wrapped(defer_secs=0.0)
        w._text = "ఆ"
        await w._on_inner_stopped(None, UserTurnStoppedParams(
            enable_user_speaking_frames=True))
        assert len(rec.stops) == 1

    asyncio.run(go())
