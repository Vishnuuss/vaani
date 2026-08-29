"""Vaani owns turn-taking -- when the caller is judged to have finished.

Why this is Vaani's and not Dograh's
------------------------------------
Turn-taking is the single largest term in the latency budget and it is a
*conversation* decision, not plumbing. Measured on run 110 (2026-08-26):

    turn 3   total 1.684s   LLM TTFB 0.270s
    turn 4   total 1.628s   LLM TTFB 0.225s
    turn 5   total 1.365s   LLM TTFB 0.196s

The LLM is no longer the problem. Roughly 1.2s per turn is spent BEFORE the LLM
starts -- endpoint decision plus STT finalisation. That is the whole remaining
gap to 700 ms, and it is decided here.

The paired constraint, from `latency_budget.yaml` and non-negotiable
-------------------------------------------------------------------
Faster endpointing buys false interruptions, and a latency win purchased with
interruptions is NOT a win. Telugu callers on the incumbent already protested
being cut off at 350 ms.

    max_false_interruption_rate: 0.02
    min_endpoint_silence_ms_telugu: 600   # hard floor when NOT using a detector
    min_turn_words: 1                     # one-word Telugu turns MUST register

`smart_turn_stop_secs` currently defaults to 0.2, which is far below that
600 ms floor. That is defensible ONLY because a trained detector is deciding
the boundary rather than a silence timer -- and only for as long as
smart-turn-v3 is actually reading Telugu prosody correctly. Nobody has measured
that. If false interruptions appear on real Telugu calls, raise this first.
"""

from __future__ import annotations

from loguru import logger

from api.services.vaani.telugu_turn import (
    TeluguTurnAnalyzer,
    TeluguTurnParams,
)
from api.schemas.workflow_configurations import (
    DEFAULT_ENDPOINT_FRAGMENT_FLOOR_SECS,
    DEFAULT_ENDPOINT_MAX_SECS,
    DEFAULT_ENDPOINT_MIN_SECS,
    DEFAULT_PROVISIONAL_VAD_PAUSE_SECS,
    DEFAULT_SMART_TURN_STOP_SECS,
    DEFAULT_TURN_START_MIN_WORDS,
    DEFAULT_TURN_START_STRATEGY,
    DEFAULT_TURN_STOP_STRATEGY,
    DEFAULT_TURN_WAIT_FOR_TRANSCRIPT,
)
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.turns.user_start import (
    ExternalUserTurnStartStrategy,
    MinWordsUserTurnStartStrategy,
    ProvisionalVADUserTurnStartStrategy,
)
from pipecat.turns.user_start.transcription_user_turn_start_strategy import (
    TranscriptionUserTurnStartStrategy,
)
from pipecat.turns.user_start.vad_user_turn_start_strategy import (
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_stop import (
    ExternalUserTurnStopStrategy,
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)


class TextAwareTurnStopStrategy(TurnAnalyzerUserTurnStopStrategy):
    """The stock strategy, plus: the analyzer is told what was said.

    Pipecat keeps the transcript to itself -- `_handle_transcription` stores it
    on the strategy and the analyzer only ever sees audio. That is the right
    default for a prosody model, and wrong here, because half of what makes a
    Telugu answer unfinished is not audible. "అరవై" (sixty) and "సరే" (fine) are
    the same shape of sound; only the words say that one of them is a quantity
    with the unit still to come.

    Overriding rather than editing pipecat keeps the submodule clean and keeps
    this decision where the rest of Vaani's turn-taking lives.
    """

    async def _handle_transcription(self, frame) -> None:
        note = getattr(self._turn_analyzer, "note_text", None)
        if note is not None:
            note(frame.text)
        await super()._handle_transcription(frame)


def resolve_turn_start_min_words(run_configs: dict) -> int:
    return max(
        1,
        int(run_configs.get("turn_start_min_words", DEFAULT_TURN_START_MIN_WORDS)),
    )


def resolve_provisional_vad_pause_secs(run_configs: dict) -> float:
    return max(
        0.1,
        float(
            run_configs.get(
                "provisional_vad_pause_secs", DEFAULT_PROVISIONAL_VAD_PAUSE_SECS
            )
        ),
    )


def create_user_turn_start_strategies(
    run_configs: dict, *, uses_external_turns: bool
):
    """When does the caller's turn BEGIN."""

    turn_start_strategy = run_configs.get(
        "turn_start_strategy", DEFAULT_TURN_START_STRATEGY
    )

    if turn_start_strategy == "min_words":
        return [
            MinWordsUserTurnStartStrategy(
                min_words=resolve_turn_start_min_words(run_configs)
            )
        ]

    if turn_start_strategy == "provisional_vad":
        return [
            ProvisionalVADUserTurnStartStrategy(
                pause_secs=resolve_provisional_vad_pause_secs(run_configs)
            ),
        ]

    if uses_external_turns:
        # The STT emits its own turn boundaries and owns interruptions. Local
        # VAD is deliberately kept out of the default start strategies: it would
        # win the race on raw voice activity and start the turn before the STT
        # confirms a real turn.
        return [ExternalUserTurnStartStrategy(enable_interruptions=True)]

    return [TranscriptionUserTurnStartStrategy(), VADUserTurnStartStrategy()]


def create_user_turn_stop_strategies(
    run_configs: dict, *, uses_external_turns: bool
):
    """When is the caller judged to have FINISHED. The expensive decision."""

    if uses_external_turns:
        return [ExternalUserTurnStopStrategy()]

    # The default MUST be supplied here. A bare .get() returned None for any
    # agent whose workflow_configurations is {} -- which is every agent created
    # through the new editor -- so the declared `turn_analyzer` default was
    # never reached and every call fell through to the fixed 0.6s speech
    # timeout below. Measured cost: a dead-constant 0.80s endpoint on runs 110
    # and 163. See test_turn_stop_default_is_applied.py.
    strategy = run_configs.get("turn_stop_strategy", DEFAULT_TURN_STOP_STRATEGY)

    if strategy == "turn_analyzer":
        stop_secs = run_configs.get(
            "smart_turn_stop_secs", DEFAULT_SMART_TURN_STOP_SECS
        )
        # Smart Turn v3 covers 23 languages and Telugu is not one of them, so on
        # a Telugu call it rarely returns COMPLETE and the turn ends on the
        # silence timeout instead of on a decision. Measured on run 213 that
        # timeout was 0.693s of a 1.05s turn -- the largest single cost, larger
        # than the LLM at 0.245s.
        #
        # TeluguTurnAnalyzer is trained on this agent's own caller recordings
        # and ends 33.9% of turns early at under a 2% false-cutoff rate. On the
        # turns it is not confident about it returns INCOMPLETE, which leaves
        # today's behaviour exactly as it is: it can make a turn faster, never
        # slower. If its weights are missing it reports `enabled = False` and we
        # fall back rather than run a detector that can never fire.
        # Two-sided endpointing. `stop_secs` stays the fallback for when the
        # model cannot load; these three are what the model spends its verdict
        # on when it can. See DEFAULT_ENDPOINT_* for why a single number was
        # never going to work.
        analyzer = TeluguTurnAnalyzer(params=TeluguTurnParams(
            stop_secs=stop_secs,
            min_endpoint_secs=float(run_configs.get(
                "endpoint_min_secs", DEFAULT_ENDPOINT_MIN_SECS)),
            max_endpoint_secs=float(run_configs.get(
                "endpoint_max_secs", DEFAULT_ENDPOINT_MAX_SECS)),
            fragment_floor_secs=float(run_configs.get(
                "endpoint_fragment_floor_secs",
                DEFAULT_ENDPOINT_FRAGMENT_FLOOR_SECS)),
        ))
        if not analyzer.enabled:
            logger.warning(
                "[turn] Telugu analyzer unavailable; falling back to Smart Turn "
                "v3, which has no Telugu and will mostly time out"
            )
            analyzer = LocalSmartTurnAnalyzerV3(
                params=SmartTurnParams(stop_secs=stop_secs))
        return [
            TextAwareTurnStopStrategy(
                turn_analyzer=analyzer,
                # Let the semantic turn detector end the turn on its own verdict
                # instead of waiting for the final transcript (measured 438 ms
                # p50 after flush). The transcript becomes bookkeeping and comes
                # off the latency critical path. `user_turn_stop_timeout` is
                # still the safety net, and SpeculationProbe measures how often
                # the text we already hold equals the final text.
                wait_for_transcript=run_configs.get(
                    "turn_wait_for_transcript", DEFAULT_TURN_WAIT_FOR_TRANSCRIPT
                ),
            )
        ]

    return [SpeechTimeoutUserTurnStopStrategy()]


def analyzer_from(strategies) -> object | None:
    """The turn analyzer inside a stop strategy, if there is one.

    The filler player needs the SAME analyzer instance the turn strategy is
    using -- it reads the probability that model just computed rather than
    running its own. Building a second one would score different audio and the
    two would disagree about whether the caller had finished.
    """
    for strategy in strategies or []:
        analyzer = getattr(strategy, "_turn_analyzer", None) or getattr(
            strategy, "turn_analyzer", None)
        if analyzer is not None:
            return analyzer
    return None
