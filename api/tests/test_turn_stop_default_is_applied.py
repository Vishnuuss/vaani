"""The turn_analyzer default was declared but never actually reached.

`DEFAULT_TURN_STOP_STRATEGY = "turn_analyzer"` was added to the schema, but
`create_user_turn_stop_strategies` read the key with a bare
``run_configs.get("turn_stop_strategy")`` -- no default. Agent 5's stored
`workflow_configurations` is `{}`, so that returned None, the comparison failed,
and every call silently fell through to `SpeechTimeoutUserTurnStopStrategy`.

That strategy has `user_speech_timeout = 0.6`. Plus Dograh's VAD `stop_secs=0.2`
that is exactly the 0.80s dead constant measured on runs 110 and 163:

    run 110  endpoint+STT  0.807  0.821  0.816  0.806
    run 163  endpoint+STT  0.804  0.807  0.802  0.815

Eight turns, two calls, +/-13ms. A trained detector's verdict would vary with
what was said. A fixed clock does not. The semantic turn analyzer had never run.
"""

from pipecat.turns.user_stop import (
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)

from api.services.vaani.turn_taking import create_user_turn_stop_strategies


def test_empty_config_uses_the_turn_analyzer_not_a_fixed_clock():
    """An agent that was never configured must still get the declared default."""
    strategies = create_user_turn_stop_strategies({}, uses_external_turns=False)

    assert len(strategies) == 1
    assert isinstance(strategies[0], TurnAnalyzerUserTurnStopStrategy), (
        "empty run_configs fell through to the fixed 0.6s speech timeout -- "
        "this is the 0.80s constant measured on runs 110 and 163"
    )


def test_an_agent_can_still_opt_out_to_the_speech_timeout():
    strategies = create_user_turn_stop_strategies(
        {"turn_stop_strategy": "transcription"}, uses_external_turns=False
    )
    assert isinstance(strategies[0], SpeechTimeoutUserTurnStopStrategy)


def test_external_turn_stt_still_wins():
    from pipecat.turns.user_stop import ExternalUserTurnStopStrategy

    strategies = create_user_turn_stop_strategies({}, uses_external_turns=True)
    assert isinstance(strategies[0], ExternalUserTurnStopStrategy)
