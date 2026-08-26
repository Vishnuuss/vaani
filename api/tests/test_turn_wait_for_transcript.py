"""Taking the transcript off the latency critical path.

Pipecat's `TurnAnalyzerUserTurnStopStrategy` already supports ending the turn on
the turn analyzer's verdict alone, without waiting for a transcript
(`wait_for_transcript=False`). Dograh left it at the default of True, so every
turn waits for the final transcript — measured at **438 ms p50** after flush.

With it False, the semantic turn detector drives the conversation and the
transcript becomes bookkeeping. That is the "don't wait for the sentence to
finish" behaviour, and it is worth ~438 ms per turn.

The trade this buys: the LLM may see a transcript that is still a word or two
short. `user_turn_stop_timeout` remains the safety net, and `SpeculationProbe`
measures how often the stable prefix already equals the final text.
"""

from api.services.pipecat.run_pipeline import (
    _create_non_realtime_user_turn_stop_strategies,
)
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)


def _strategy(run_configs):
    strategies = _create_non_realtime_user_turn_stop_strategies(
        run_configs, uses_external_turns=False
    )
    return strategies[0]


def test_the_transcript_is_off_the_critical_path_by_default():
    strategy = _strategy({"turn_stop_strategy": "turn_analyzer"})

    assert isinstance(strategy, TurnAnalyzerUserTurnStopStrategy)
    assert strategy.wait_for_transcript is False


def test_waiting_for_the_transcript_can_be_turned_back_on():
    strategy = _strategy(
        {"turn_stop_strategy": "turn_analyzer", "turn_wait_for_transcript": True}
    )

    assert strategy.wait_for_transcript is True
