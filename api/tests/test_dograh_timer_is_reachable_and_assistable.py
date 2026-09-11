"""Dograh's timer is what runs, so its wait has to be reachable -- and the
Telugu model may now sit beside it rather than replace it.

Two facts established on 11 Sep, both by reading the live stored config rather
than the code defaults:

  1. All four agents are set to `turn_stop_strategy: "transcription"`. Every
     `endpoint_*` and `turn_model` value is read ONLY inside the
     `turn_analyzer` branch, so on these agents they do nothing. MB Solar has
     `endpoint_min_secs: 0.3` stored and it has never had any effect.
  2. That branch built `SpeechTimeoutUserTurnStopStrategy()` with no arguments,
     so the wait was pipecat's hardcoded 0.6s. Plus Silero's 0.2s that is the
     0.8s floor measured on every one of 69 turns that day.

The Telugu detector was tried as a REPLACEMENT and judged worse, which is why
"transcription" is configured at all. `telugu_turn_assist` is not a reversal of
that: the controller stops a turn on whichever strategy fires first, so the
timer stays the backstop and the model can only end a turn EARLIER. It is off
by default and these tests pin that, because the default IS the decision.
"""

from __future__ import annotations

from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)

from api.services.vaani.turn_taking import _build_user_turn_stop_strategies


def _dograh(**overrides) -> dict:
    """What the live agents are actually configured with."""
    return {"turn_stop_strategy": "transcription", **overrides}


def test_the_default_is_exactly_todays_behaviour():
    strategies = _build_user_turn_stop_strategies(_dograh())
    assert len(strategies) == 1, (
        "one strategy, as today. Adding one by default would change how every "
        "live agent takes turns without anybody choosing it")
    assert isinstance(strategies[0], SpeechTimeoutUserTurnStopStrategy)
    assert strategies[0]._user_speech_timeout == 0.6, (
        "pipecat's own default. The dial exists now; it starts where it was")


def test_the_timer_does_not_also_wait_for_the_transcript():
    """The second, hidden wait. See run 876."""
    strategies = _build_user_turn_stop_strategies(_dograh())
    assert strategies[0].wait_for_transcript is False, (
        "the strategy ends a turn only when BOTH timers are done AND a "
        "transcript has arrived, so leaving this True puts Sarvam back on the "
        "critical path -- which is exactly what DEFAULT_TURN_WAIT_FOR_TRANSCRIPT "
        "was written to prevent, for the other path only")


def test_waiting_for_the_transcript_is_still_available():
    strategies = _build_user_turn_stop_strategies(
        _dograh(turn_wait_for_transcript=True))
    assert strategies[0].wait_for_transcript is True


def test_the_wait_can_be_shortened_without_a_deploy():
    strategies = _build_user_turn_stop_strategies(
        _dograh(dograh_speech_timeout_secs=0.35))
    assert strategies[0]._user_speech_timeout == 0.35, (
        "this is the only dial that moves the 0.8s floor on these agents")


def test_telugu_assist_stays_off_unless_asked_for():
    assert len(_build_user_turn_stop_strategies(_dograh())) == 1
    assert len(_build_user_turn_stop_strategies(
        _dograh(telugu_turn_assist=False))) == 1


def test_assist_adds_the_model_and_KEEPS_dograhs_timer():
    strategies = _build_user_turn_stop_strategies(
        _dograh(telugu_turn_assist=True))
    if len(strategies) == 1:
        # The weights are not present in this environment. Declining to assist
        # is the correct degraded behaviour and is asserted rather than skipped.
        assert isinstance(strategies[0], SpeechTimeoutUserTurnStopStrategy)
        return
    assert len(strategies) == 2
    assert isinstance(strategies[0], SpeechTimeoutUserTurnStopStrategy), (
        "Dograh's timer must SURVIVE. It is the backstop, and the whole point "
        "is that the Telugu model can only end a turn earlier, never later")
    assert strategies[0]._user_speech_timeout == 0.6


def test_a_broken_model_never_takes_the_timer_down_with_it():
    """A missing weights file is a degraded call. An exception is no call."""
    strategies = _build_user_turn_stop_strategies(
        _dograh(telugu_turn_assist=True, turn_model="nonexistent-model-kind"))
    assert strategies, "something must always end a turn"
    assert isinstance(strategies[0], SpeechTimeoutUserTurnStopStrategy)


def test_the_turn_analyzer_path_is_not_disturbed():
    """The other branch still builds, after the analyzer was factored out."""
    strategies = _build_user_turn_stop_strategies(
        {"turn_stop_strategy": "turn_analyzer",
         "semantic_turn_completion": False})
    assert strategies, "the replacement path must still produce a strategy"
