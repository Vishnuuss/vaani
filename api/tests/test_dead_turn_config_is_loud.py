"""A turn key that is set, believed, and silently ignored must say so.

This has now cost this project three separate diagnoses, and the code already
carries the scar tissue in a comment: "Someone set MB Solar's
`endpoint_min_secs` to 0.3 and it changed nothing."

On 23 September MB Solar was running with `semantic_turn_completion: True`
stored on the workflow, and anyone reading that config believed the LLM was
gating turn ends. It was not. The key is read ONLY inside the `turn_analyzer`
branch, and all four agents are on `"transcription"`, so the turn was ended by
whichever of the 0.7s timer and the eager Telugu assist fired first. A caller
answering "మాది." and drawing breath lost the floor after two words, and run
1012 shows the cost: the LLM started three replies, each cancelled by the
caller still talking, so he heard nothing at all and hung up saying
"హలో. హలో అండి".

The stored value was wrong. The reason it stayed wrong is that it failed
SILENTLY, so these tests are about the silence, not the value.
"""

from __future__ import annotations

import pytest
from loguru import logger

from api.services.vaani.turn_taking import _build_user_turn_stop_strategies

# Keys the `turn_analyzer` branch alone consults. On a "transcription" agent
# every one of them is inert, and every one of them reads like a decision.
DEAD_ON_TRANSCRIPTION = (
    "semantic_turn_completion",
    "turn_model",
    "endpoint_min_secs",
    "endpoint_max_secs",
    "endpoint_unsure_floor_secs",
    "endpoint_unsure_band",
    "smart_turn_stop_secs",
)


@pytest.fixture
def warned():
    """This codebase logs through loguru, which never reaches pytest's caplog.

    A first version of these tests asserted against `caplog` and passed
    vacuously on an empty string -- a check that looks like it is watching and
    is not, which is the same failure these tests exist to stop.
    """
    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="WARNING")
    try:
        yield captured
    finally:
        logger.remove(sink)


def test_semantic_turn_completion_on_the_timer_path_is_announced(warned):
    """The exact live configuration of MB Solar on 23 Sep."""
    _build_user_turn_stop_strategies({
        "turn_stop_strategy": "transcription",
        "semantic_turn_completion": True,
    })
    text = "\n".join(warned)
    assert "semantic_turn_completion" in text, (
        "the key was set, it did nothing, and nothing said so")
    assert "turn_analyzer" in text, (
        "the warning has to name the setting that would make it live, or the "
        "reader is left knowing only that they are wrong")


def test_every_analyzer_only_key_is_named(warned):
    _build_user_turn_stop_strategies({
        "turn_stop_strategy": "transcription",
        **{k: 1 for k in DEAD_ON_TRANSCRIPTION},
    })
    text = "\n".join(warned)
    missing = [k for k in DEAD_ON_TRANSCRIPTION if k not in text]
    assert not missing, f"silently ignored and never named: {missing}"


def test_the_analyzer_path_says_nothing(warned):
    """No warning when the keys are live. A warning that fires on a correct
    config is noise, and noise is how the real one gets missed."""
    _build_user_turn_stop_strategies({
        "turn_stop_strategy": "turn_analyzer",
        "semantic_turn_completion": True,
    })
    assert "semantic_turn_completion" not in "\n".join(warned)


def test_a_plain_timer_agent_says_nothing(warned):
    """Nor when the operator never asked for any of it."""
    _build_user_turn_stop_strategies({"turn_stop_strategy": "transcription"})
    assert "ignored" not in "\n".join(warned).lower()
