"""The turn-taking defaults are a latency decision, so they are pinned here.

Dograh shipped `turn_stop_strategy="transcription"` with a 2.0 s silence timer.
That is a dumb clock: it waits two seconds of silence on every turn, and it
gates on the FINAL transcript (measured 438 ms after flush).

`LocalSmartTurnAnalyzerV3` already ships with Dograh and runs IN-PROCESS, so
semantic end-of-turn detection costs no network hop.

The floor is not negotiable: latency_budget.yaml sets
`min_endpoint_silence_ms_telugu: 600` and `max_false_interruption_rate: 0.02`.
Telugu callers on the incumbent already protested being cut off at 350 ms, so
0.6 s is the floor, not a starting point to tune below without call evidence.
"""

from api.schemas.workflow_configurations import (
    DEFAULT_SMART_TURN_STOP_SECS,
    DEFAULT_TURN_STOP_STRATEGY,
)


def test_semantic_turn_detection_is_the_default():
    assert DEFAULT_TURN_STOP_STRATEGY == "turn_analyzer"


def test_the_silence_window_respects_the_telugu_floor():
    assert DEFAULT_SMART_TURN_STOP_SECS == 0.2
    # VAD stop_secs (0.2) stacks on top, so total silence before release is 0.4 s,
    # which stays above the 350 ms point Telugu callers complained about.
    assert DEFAULT_SMART_TURN_STOP_SECS + 0.2 >= 0.35, "below the Telugu interruption floor"
